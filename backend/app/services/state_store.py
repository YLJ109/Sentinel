"""带 TTL 的运行时状态存储。

用途：报警冷却、事件去重这类"短生命周期、需跨进程一致"的状态。
原先放在进程内字典里，多 worker 或多实例部署时各算各的，会重复报警；
改落 SQLite 后，同一套状态被所有 worker 共享，重启也不丢。

性能考量：只在"行为已通过多帧投票"这类低频时机读写，不会成为热路径瓶颈。
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.db import SessionLocal
from app.models import RuntimeState

log = logging.getLogger("services.state")

GC_INTERVAL_SEC = 600


class StateStore:
    """基于 SQLite 的键值状态存储（带过期时间）。"""

    async def set(self, key: str, value: str = "1", ttl: float = 3600.0) -> None:
        expires = time.time() + ttl
        async with SessionLocal() as db:
            stmt = sqlite_insert(RuntimeState).values(key=key, value=str(value), expires_at=expires)
            stmt = stmt.on_conflict_do_update(
                index_elements=[RuntimeState.key],
                set_={"value": stmt.excluded.value, "expires_at": stmt.excluded.expires_at},
            )
            await db.execute(stmt)
            await db.commit()

    async def get(self, key: str) -> str | None:
        async with SessionLocal() as db:
            row = await db.get(RuntimeState, key)
            if row is None:
                return None
            if row.expires_at and row.expires_at < time.time():
                return None
            return row.value

    async def get_float(self, key: str, default: float = 0.0) -> float:
        raw = await self.get(key)
        try:
            return float(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    async def purge_expired(self) -> int:
        async with SessionLocal() as db:
            res = await db.execute(delete(RuntimeState).where(RuntimeState.expires_at < time.time()))
            await db.commit()
            return res.rowcount or 0

    async def gc_loop(self) -> None:
        """周期回收过期键，避免表持续膨胀。"""
        while True:
            try:
                removed = await self.purge_expired()
                if removed:
                    log.debug("运行时状态回收 %s 条", removed)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("运行时状态回收失败：%s", e)
            await asyncio.sleep(GC_INTERVAL_SEC)


state_store = StateStore()
