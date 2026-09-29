"""异步数据库引擎、会话管理与轻量迁移。"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.sqlite_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# 增量列定义：(表名, 列名, 类型 DDL)。SQLite 不支持完整 ALTER，按需补列即可。
_PENDING_COLUMNS: list[tuple[str, str, str]] = [
    ("detection_events", "track_ids", "VARCHAR(255)"),
    ("detection_events", "detail", "TEXT"),
    ("alarm_records", "camera_id", "INTEGER"),
    ("alarm_records", "handler_id", "INTEGER"),
    ("alarm_records", "feedback_label", "VARCHAR(32)"),
    ("alarm_records", "ignored_reason", "VARCHAR(255)"),
    ("chat_logs", "is_final", "BOOLEAN DEFAULT 1"),
    ("cameras", "device_id", "VARCHAR(255)"),
    ("cameras", "code", "VARCHAR(64)"),
]


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """建表 + 增量补列（首次启动 / 版本升级均可安全执行）。"""
    from app.models.base import Base
    import app.models  # noqa: F401  确保模型注册到 Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)
        await conn.execute(text("PRAGMA journal_mode=WAL"))


async def _migrate(conn) -> None:
    """为已存在的库补齐新增列，避免升级时必须重建数据库。"""
    for table, column, ddl in _PENDING_COLUMNS:
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
        if not rows:
            continue  # 表尚未创建（由 create_all 负责）
        existing = {r[1] for r in rows}
        if column not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
