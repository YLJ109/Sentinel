"""视频文件检测流水线：逐帧真实推理 → 事件/报警落库 → 标注视频 + 报警片段输出。

与实时链路的差异：
- 视频文件不限频（``force=True``），按抽样步长逐帧推进，保证离线分析精度
- 使用独立的 ``pipeline_key``，不会重置同一摄像头的实时跟踪状态
- 输出带检测框/骨架/行为标签的标注视频，并在报警时刻裁出前后片段
- 进度写入数据库（而非内存），进程重启后前端仍能正确显示

并发控制：GPU 显存有限，同名任务通过信号量串行执行（``VIDEO_WORKER_CONCURRENCY``），
避免同时上传多个视频时相互挤占显存导致 OOM。
CPU 密集的解码/推理/编码通过 ``asyncio.to_thread`` 卸载到线程池，
避免长时间占用事件循环导致 API 无响应。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import cv2

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import VideoRecord
from app.services import alarm as alarm_svc
from app.services.clips import cut_from_video
from app.vision.draw import annotate
from app.vision.engine import engine, video_pipeline_key

log = logging.getLogger("services.video")

SAMPLE_INTERVAL_SEC = 0.33

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

        step = max(1, int(round(fps * SAMPLE_INTERVAL_SEC)))
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

        try:
            while True:
                ok, frame = await asyncio.to_thread(cap.read)
                if not ok:
                    break

                if frame_idx % step == 0:
                    tsec = frame_idx / fps if fps else 0.0
                    result = await asyncio.to_thread(
                        engine.infer, frame, rec.camera_id, tsec, True, key
                    )

                    event_count += len(result.behaviors)
                    triggered = await alarm_svc.process_frame(
                        db, rec.camera_id, frame, result,
                        operator_id=rec.uploaded_by, frame_time=tsec,
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

                    if writer is not None:
                        drawn = await asyncio.to_thread(annotate, frame, result.tracks, result.behaviors)
                        await asyncio.to_thread(writer.write, drawn)

                    processed += 1
                    if total and processed % 10 == 0:
                        rec.progress = min(1.0, frame_idx / total)
                        await db.commit()

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
        log.info("视频检测完成 id=%s 事件=%s 报警=%s 输出=%s",
                 rec.id, event_count, alarm_count, rec.processed_path)
