"""仪表盘统计。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models import AlarmRecord, Camera, DetectionEvent, User
from app.schemas import AlarmOut, DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"])


@router.get("/stats", response_model=DashboardStats)
async def stats(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    cam_total = (await db.execute(select(func.count()).select_from(Camera))).scalar() or 0
    cam_online = (await db.execute(
        select(func.count()).select_from(Camera).where(Camera.enabled.is_(True)))).scalar() or 0
    today_events = (await db.execute(
        select(func.count()).select_from(DetectionEvent).where(DetectionEvent.created_at >= since))).scalar() or 0
    today_alarms = (await db.execute(
        select(func.count()).select_from(AlarmRecord).where(AlarmRecord.created_at >= since))).scalar() or 0
    pending = (await db.execute(
        select(func.count()).select_from(AlarmRecord).where(AlarmRecord.status == "pending"))).scalar() or 0

    rows = (await db.execute(
        select(DetectionEvent.event_type, func.count())
        .where(DetectionEvent.created_at >= since)
        .group_by(DetectionEvent.event_type))).all()
    breakdown = {et: c for et, c in rows}

    recent = list((await db.execute(
        select(AlarmRecord).order_by(AlarmRecord.id.desc()).limit(8))).scalars().all())
    return DashboardStats(
        camera_online=cam_online, camera_total=cam_total,
        today_events=today_events, today_alarms=today_alarms, pending_alarms=pending,
        event_type_breakdown=breakdown,
        recent_alarms=[AlarmOut.model_validate(a) for a in recent],
    )
