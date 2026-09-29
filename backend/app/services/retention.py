"""留存策略：按保留天数清理过期的事件、报警、转写与取证文件。

设计要点：
- 先删数据库行（EvidenceFile → AlarmRecord/DetectionEvent/ChatLog/VideoRecord），
  再按文件修改时间清理磁盘，避免出现"文件已删但记录还在"的悬空引用。
- 同时清理未被数据库引用的孤儿文件（例如分析中途失败留下的半成品）。
- 清理是幂等的，可安全重复执行；单次执行结果写日志，便于审计。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import AlarmRecord, ChatLog, DetectionEvent, EvidenceFile, VideoRecord

log = logging.getLogger("services.retention")

# 需要按 mtime 清理的目录
_MEDIA_DIRS = ("evidence", "clips", "uploads")


async def purge_once(days: int | None = None) -> dict:
    """执行一次过期清理，返回统计信息。"""
    days = settings.EVIDENCE_KEEP_DAYS if days is None else days
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = time.time() - days * 86400
    # SQLite 中 server_default now() 存的是 naive 时间，比较时统一用 naive
    cutoff_naive = cutoff_dt.replace(tzinfo=None)

    stats: dict[str, int] = {}

    async with SessionLocal() as db:
        # 顺序：先删子表，再删主表，避免悬空外键
        for model, column in (
            (EvidenceFile, EvidenceFile.created_at),
            (AlarmRecord, AlarmRecord.created_at),
            (DetectionEvent, DetectionEvent.created_at),
            (ChatLog, ChatLog.created_at),
            (VideoRecord, VideoRecord.created_at),
        ):
            res = await db.execute(delete(model).where(column < cutoff_naive))
            stats[model.__tablename__] = res.rowcount or 0
        await db.commit()

    files, freed = _purge_files(cutoff_ts)
    stats["files"] = files
    stats["freed_mb"] = round(freed / 1024 / 1024, 2)
    stats["cutoff_days"] = days

    if files or any(v for k, v in stats.items() if k != "cutoff_days"):
        log.info("留存清理完成：%s", stats)
    return stats


def _purge_files(cutoff_ts: float) -> tuple[int, int]:
    """按修改时间删除过期媒体文件与孤儿文件。"""
    removed = 0
    freed = 0
    root = Path(settings.DATA_DIR)
    for sub in _MEDIA_DIRS:
        folder = root / sub
        if not folder.is_dir():
            continue
        for f in folder.rglob("*"):
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff_ts:
                try:
                    size = f.stat().st_size
                    f.unlink()
                    removed += 1
                    freed += size
                except OSError as e:
                    log.warning("删除过期文件失败 %s: %s", f, e)
    return removed, freed


async def retention_loop() -> None:
    """周期执行留存清理。启动后先等 60s，避免与模型预热争抢 IO。"""
    await asyncio.sleep(60)
    while True:
        try:
            await purge_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("留存清理失败：%s", e)
        await asyncio.sleep(max(600, settings.RETENTION_INTERVAL_SEC))
