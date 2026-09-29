"""进程内日志环形缓冲：让控制台能直接看到后端异常，而不必登录服务器翻日志文件。

设计要点：
- 以 ``logging.Handler`` 形式挂到根 logger，因此**所有**模块的日志自动进入缓冲，
  无需在每个调用点埋点。
- 固定容量（默认 600 条），超出自动淘汰最旧的，内存占用有上界。
- 过滤 uvicorn 访问日志，避免把每条 HTTP 请求刷进"异常看板"。
- 前端异常（window.onerror / Vue errorHandler）也归集进来，
  这样"哪里出错了"只有一个入口，不用在两个地方找。
- emit 内部再次触发日志会造成递归，用线程本地标志位自我保护。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

CAPACITY = 600

# 记录异常时忽略这些 logger，它们噪音大且对排障无价值
_IGNORED_LOGGERS = ("uvicorn.access", "asyncio", "watchfiles", "watchgod")
# 只有这些级别进入缓冲
_KEEP_LEVELS = (logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)


class RingLogHandler(logging.Handler):
    """把日志记录写入固定容量的环形缓冲。"""

    def __init__(self, capacity: int = CAPACITY, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._guard = threading.local()
        self.error_count = 0
        self.warning_count = 0

    # ---------- 写入 ----------
    def emit(self, record: logging.LogRecord) -> None:
        # 防止 emit 内部再写日志导致无限递归
        if getattr(self._guard, "busy", False):
            return
        if record.levelno not in _KEEP_LEVELS:
            return
        if any(record.name.startswith(p) for p in _IGNORED_LOGGERS):
            return

        self._guard.busy = True
        try:
            tb = None
            if record.exc_info:
                tb = self.format(record)
            with self._lock:
                self._seq += 1
                self._buffer.append({
                    "id": self._seq,
                    "ts": round(record.created, 3),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "traceback": tb,
                })
            if record.levelno >= logging.ERROR:
                self.error_count += 1
            elif record.levelno == logging.WARNING:
                self.warning_count += 1
        except Exception:  # 日志失败绝不能影响主流程
            pass
        finally:
            self._guard.busy = False

    # ---------- 查询 ----------
    def snapshot(self, level: str | None = None, offset: int = 0,
                 limit: int = 200, since_id: int = 0) -> dict[str, Any]:
        """按级别与游标读取日志，最新的在前。"""
        wanted = None
        if level:
            wanted = {"ERROR": ("ERROR", "CRITICAL"), "WARN": ("WARNING",),
                      "INFO": ("INFO",)}.get(level.upper())

        with self._lock:
            items = list(self._buffer)

        if wanted:
            items = [e for e in items if e["level"] in wanted]
        if since_id:
            items = [e for e in items if e["id"] > since_id]
        items.reverse()  # 最新在前

        total = len(items)
        return {
            "items": items[offset:offset + limit],
            "total": total,
            "latest_id": self._seq,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
        }

    def push_client(self, level: str, message: str, source: str = "frontend",
                    traceback: str | None = None) -> None:
        """记录一条来自前端的异常。"""
        lvl = (level or "error").upper()
        if lvl not in ("INFO", "WARNING", "ERROR", "CRITICAL"):
            lvl = "ERROR"
        self._guard.busy = True
        try:
            with self._lock:
                self._seq += 1
                self._buffer.append({
                    "id": self._seq,
                    "ts": round(time.time(), 3),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "level": lvl,
                    "logger": f"frontend.{source}",
                    "message": message,
                    "traceback": traceback,
                })
            if lvl in ("ERROR", "CRITICAL"):
                self.error_count += 1
            else:
                self.warning_count += 1
        finally:
            self._guard.busy = False

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self.error_count = 0
            self.warning_count = 0


log_buffer = RingLogHandler()


def install(level: int = logging.INFO) -> RingLogHandler:
    """挂载到根 logger（幂等）。"""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingLogHandler):
            return log_buffer
    log_buffer.setLevel(level)
    root.addHandler(log_buffer)
    return log_buffer
