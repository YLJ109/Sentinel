"""报警视频片段：实时流预录 + 视频文件裁剪。

实时链路没有可回退的时间轴，因此每个点位维护一个 JPEG 环形缓冲（预录），
报警时把缓冲快照冻结成剪辑起点，后续帧继续追加，直到满足后延时长再落盘。

为什么缓冲存 JPEG 而不是原始帧：
3s 预录 + 5s 后延 @12fps ≈ 96 帧，原始 BGR 约 66MB/路，JPEG 约 4MB/路，
在 8~16 路并发的部署下这个差别直接决定内存是否够用。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from app.core.config import settings

log = logging.getLogger("services.clips")


class ClipRecorder:
    """按点位维护预录环形缓冲，并在报警时生成前后片段。

    并发与时钟约定（两处都是曾经的隐患，务必保持）：

    1. ``push()`` 由 ``asyncio.to_thread`` 投递到线程池执行，而 ``start()`` /
       ``reset()`` 在事件循环线程执行，两者会同时读写 ``_rings`` / ``_active``。
       因此所有状态变更都在 ``self._lock`` 内完成，避免"迭代中变更"与同段剪辑被写两次。
    2. 时间戳一律取 ``time.monotonic()``。曾经 ``push`` 用浏览器上报的时间戳、
       ``start`` 用服务器时间戳，两者基准不同：只要浏览器时钟慢于服务器，
       ``ts < end_ts`` 就永远成立，进行中的剪辑永不落盘、帧在内存里持续堆积，同时
       剪辑也永远不会出现在报警详情里。
    """

    def __init__(self) -> None:
        self._capacity = max(8, settings.CLIP_RING_CAPACITY)
        self._rings: dict[int, deque[tuple[float, bytes]]] = {}
        self._active: dict[int, dict] = {}
        self._lock = threading.Lock()
        # 已完成、待调用方登记的剪辑。放在队列里由 push() 顺带取走，
        # 这样"上一条未补完后延就被新报警抢占"的剪辑不会被静默丢弃。
        self._done: deque[tuple[int, str]] = deque(maxlen=32)

    # ---------- 帧输入 ----------
    def push(self, camera_id: int | None, frame_bgr: np.ndarray) -> tuple[int, str] | None:
        """存入缓冲并推进进行中的剪辑。

        返回 ``(alarm_id, 相对路径)`` 表示某条剪辑刚好完成，调用方负责登记到报警。
        """
        jpeg = self._encode(frame_bgr)
        if jpeg is None:
            return None
        now = self._now()

        with self._lock:
            done = self._done.popleft() if self._done else None
            if camera_id is None:
                return done

            ring = self._rings.setdefault(camera_id, deque(maxlen=self._capacity))
            ring.append((now, jpeg))

            active = self._active.get(camera_id)
            if active is None:
                return done

            active["frames"].append((now, jpeg))
            # 时间兜底：既然后延时长已过就落盘；若调用方长时间不再送帧，
            # 该条也会在 deadline 之后被回收，避免帧一直挂在内存里。
            if now < active["end_ts"] and now < active["deadline"]:
                return done

            self._active.pop(camera_id, None)
            path = self._write(active["frames"], active["alarm_id"])

        finished = (active["alarm_id"], path) if path else None
        if finished is None:
            return done
        if done is None:
            return finished
        with self._lock:
            self._done.append(finished)
        return done

    def start(self, camera_id: int | None, alarm_id: int) -> bool:
        """报警触发：用当前环形缓冲作为预录，开启后延采集。"""
        if camera_id is None:
            return False
        now = self._now()
        with self._lock:
            prev = self._active.pop(camera_id, None)
            if prev is not None:
                # 同一路在上一条剪辑还没补完后延帧时又触发报警：
                # 先把上一条落盘并挂入待登记队列（原实现直接覆盖，会静默丢弃）。
                path = self._write(prev["frames"], prev["alarm_id"])
                if path:
                    self._done.append((prev["alarm_id"], path))
            pre = list(self._rings.get(camera_id, ()))
            self._active[camera_id] = {
                "alarm_id": alarm_id,
                "frames": pre,
                "end_ts": now + settings.CLIP_POST_SECONDS,
                # 后延正常应在此刻前收满；超出则说明送帧已中断，允许落盘回收
                "deadline": now + settings.CLIP_POST_SECONDS + 15.0,
            }
            pending = len(pre)
        log.info("报警 #%s 已开启剪辑录制：预录 %s 帧，后延 %ss",
                 alarm_id, pending, settings.CLIP_POST_SECONDS)
        return True

    def reset(self, camera_id: int | None) -> None:
        if camera_id is None:
            return
        with self._lock:
            self._rings.pop(camera_id, None)
            self._active.pop(camera_id, None)

    # ---------- 内部 ----------
    @staticmethod
    def _now() -> float:
        """剪辑内部的统一时基（单调时钟），只用于计算间隔与时长。"""
        return time.monotonic()

    @staticmethod
    def _encode(frame: np.ndarray) -> bytes | None:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None

    @staticmethod
    def _write(frames: list[tuple[float, bytes]], alarm_id: int) -> str | None:
        if len(frames) < 2:
            return None
        first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
        if first is None:
            return None
        h, w = first.shape[:2]

        # 帧率按实际到达间隔反算：浏览器送帧频率与配置值未必一致，
        # 固定用配置帧率会导致回放快进/慢放。
        span = frames[-1][0] - frames[0][0]
        fps = (len(frames) - 1) / span if span > 0.2 else float(settings.CLIP_FPS)
        fps = max(1.0, min(30.0, fps))

        name = f"alarm_{alarm_id}_{int(time.time() * 1000)}.mp4"
        out = Path(settings.CLIP_DIR) / name
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not writer.isOpened():
            log.warning("剪辑写入器初始化失败：%s", out)
            return None
        try:
            for _, jpeg in frames:
                img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    writer.write(img)
        finally:
            writer.release()
        log.info("剪辑已生成：%s（%s 帧 @%.1f fps，跨度 %.1fs）", name, len(frames), fps, span)
        return f"clips/{name}"


def cut_from_video(src: str, center_sec: float, out_name: str,
                   pre: int | None = None, post: int | None = None) -> str | None:
    """从视频文件按时间点裁剪报警片段（视频检测用，时间轴精确）。"""
    pre = pre if pre is not None else settings.CLIP_PRE_SECONDS
    post = post if post is not None else settings.CLIP_POST_SECONDS
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if not w or not h:
        cap.release()
        return None

    start = max(0.0, center_sec - pre)
    end = center_sec + post
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)

    out = Path(settings.CLIP_DIR) / out_name
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        cap.release()
        return None
    try:
        while True:
            pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if pos > end:
                break
            ok, frame = cap.read()
            if not ok:
                break
            writer.write(frame)
    finally:
        writer.release()
        cap.release()
    return f"clips/{out_name}" if out.exists() else None


clip_recorder = ClipRecorder()
