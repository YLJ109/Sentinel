"""视频文件检测流水线：逐帧真实推理 → 事件/报警落库 → 标注视频 + 报警片段输出。

与实时链路的差异：
- 视频文件不限频（``force=True``），采样由画面运动自适应决定，静止稀疏、动作密集
- 使用独立的 ``pipeline_key``，不会重置同一摄像头的实时跟踪状态
- 输出带检测框/骨架/行为标签的标注视频（逐帧写入，时长与原片一致）
- 进度写入数据库（而非内存），进程重启后前端仍能正确显示

并发控制：GPU 显存有限，同名任务通过信号量串行执行（``VIDEO_WORKER_CONCURRENCY``），
避免同时上传多个视频时相互挤占显存导致 OOM。
CPU 密集的解码/推理/编码通过 ``asyncio.to_thread`` 卸载到线程池，
避免长时间占用事件循环导致 API 无响应。
"""
from __future__ import annotations

import asyncio
import logging

import cv2
import numpy as np

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import VideoRecord
from app.services import alarm as alarm_svc
from app.services.clips import cut_from_video
from app.vision.draw import annotate
from app.vision.engine import engine, video_pipeline_key

log = logging.getLogger("services.video")


def _small_gray(frame):
    """把画面缩到 160x90 灰度，用于计算帧间运动量。

    这里刻意用极低分辨率：运动度量只需要"有没有在动"这个粗判断，
    全分辨率做帧差的代价会和推理本身一个量级，反而抵消了自适应采样的收益。
    """
    return cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90),
                      interpolation=cv2.INTER_AREA)


# 全局并发闸门：默认串行，避免多视频同时抢显存
_worker_gate = asyncio.Semaphore(max(1, settings.VIDEO_WORKER_CONCURRENCY))


def _open(path: str):
    cap = cv2.VideoCapture(path)
    return cap if cap.isOpened() else None


async def process_video(rec_id: int) -> None:
    """后台任务：完整分析一个已上传的视频记录。"""
    async with _worker_gate:
        await _process(rec_id)


async def _process(rec_id: int) -> None:
    key = video_pipeline_key(rec_id)
    async with SessionLocal() as db:
        rec = await db.get(VideoRecord, rec_id)
        if rec is None:
            return

        cap = await asyncio.to_thread(_open, rec.original_path)
        if cap is None:
            rec.status = "failed"
            await db.commit()
            log.warning("视频无法打开：%s", rec.original_path)
            return

        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        rec.duration = round(total / fps, 2) if fps else 0.0

        out_name = f"annotated_{rec.id}.mp4"
        out_path = settings.CLIP_DIR / out_name
        writer = await asyncio.to_thread(
            lambda: cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                    fps, (width, height)) if width and height else None
        )

        event_count = 0
        alarm_count = 0
        frame_idx = 0
        processed = 0
        # ---- 自适应采样状态 ----
        prev_gray = None
        last_infer_t = -1e9
        active_until = -1.0
        last_result = None

        try:
            while True:
                ok, frame = await asyncio.to_thread(cap.read)
                if not ok:
                    break

                tsec = frame_idx / fps if fps else 0.0

                # ---- 运动自适应的采样决策 ----
                # 固定间隔（原实现 0.33s）会整段跳过短促动作：实测一段 8 秒的
                # 推搡只持续约 0.35 秒（8 帧），正好落在两个采样点之间被完全略过，
                # 结果"有人被打"的片段只剩下围观与倒地。改为按画面运动自适应：
                # 静止时稀疏采样保速度，一旦有动作就转入密集采样并把活跃期延长，
                # 既不漏掉短促冲突，也不把算力浪费在没人的静止画面上。
                small = await asyncio.to_thread(_small_gray, frame)
                if prev_gray is None:
                    motion = 1.0
                else:
                    motion = float(np.abs(small - prev_gray).mean()) / 255.0
                prev_gray = small
                if motion >= settings.VIDEO_MOTION_THRESHOLD:
                    active_until = tsec + settings.VIDEO_ACTIVE_HOLD_SEC
                active = tsec <= active_until
                interval = (settings.VIDEO_SAMPLE_ACTIVE_SEC if active
                            else settings.VIDEO_SAMPLE_IDLE_SEC)

                if tsec - last_infer_t >= interval:
                    result = await asyncio.to_thread(
                        engine.infer, frame, rec.camera_id, tsec, True, key
                    )
                    last_infer_t = tsec
                    last_result = result

                    event_count += len(result.behaviors)
                    triggered = await alarm_svc.process_frame(
                        db, rec.camera_id, frame, result,
                        operator_id=rec.uploaded_by, frame_time=tsec,
                        video_id=rec.id,
                    )
                    if triggered:
                        alarm_count += 1
                        # 视频有时间轴，直接按时间点裁剪报警片段（精确且开销小）
                        clip = await asyncio.to_thread(
                            cut_from_video, rec.original_path, tsec,
                            f"alarm_{triggered['alarm_id']}_{rec.id}.mp4",
                        )
                        if clip:
                            await alarm_svc.attach_clip(db, triggered["alarm_id"], clip)

                    processed += 1
                    if total and processed % 10 == 0:
                        rec.progress = min(1.0, frame_idx / total)
                        await db.commit()

                # 标注视频必须**逐帧写入**：只写采样帧的话，成片帧数只有原片的
                # 1/step，却仍按原始帧率封装，播放起来会比原片快 step 倍
                # （10 分钟的上传会变成 75 秒），完全无法用于回看。
                # 未采样的帧沿用上一次的推理结果；由于静止帧本来就不触发采样，
                # 这种沿用恰好只发生在画面没动的时候，观感上不会有框滞留。
                if writer is not None:
                    src = last_result
                    drawn = await asyncio.to_thread(
                        annotate, frame,
                        src.tracks if src else [], src.behaviors if src else [],
                    )
                    await asyncio.to_thread(writer.write, drawn)

                frame_idx += 1
        finally:
            await asyncio.to_thread(cap.release)
            if writer is not None:
                await asyncio.to_thread(writer.release)

        rec.event_count = event_count
        rec.alarm_count = alarm_count
        rec.processed_path = f"clips/{out_name}" if writer is not None else None
        rec.status = "done"
        rec.progress = 1.0
        await db.commit()

        # 只回收该视频自己的管线，绝不动实时链路的跟踪状态
        engine.reset(key)
        # 回收该视频在离线去重表里的条目（内存状态，任务结束即无用）
        alarm_svc.clear_offline_state(rec.id)
        log.info("视频检测完成 id=%s 事件=%s 报警=%s 输出=%s",
                 rec.id, event_count, alarm_count, rec.processed_path)
