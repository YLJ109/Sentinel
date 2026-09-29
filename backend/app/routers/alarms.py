"""报警记录管理（取证与处置核心）。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.core.timeutil import parse_dt_query
from app.models import AlarmRecord, User
from app.schemas import AlarmOut, AlarmPage, AlarmUpdate

router = APIRouter(prefix="/api/alarms", tags=["报警"])

VALID_STATUS = ("pending", "handling", "resolved", "ignored")
VALID_FEEDBACK = ("false_positive", "not_bullying", "confirmed", "other")

# 处置类写操作为值班长/管理员职责，viewer 只读
_handler = require_role("admin", "operator")

# 时间参数解析统一走公共实现（原先本文件与 history.py 各复制了一份相同逻辑）
_parse_dt = parse_dt_query


@router.get("", response_model=AlarmPage)
async def list_alarms(
    status: str | None = Query(None),
    level: str | None = Query(None),
    source: str | None = Query(None),
    camera_id: int | None = Query(None),
    date_from: str | None = Query(None, description="起始时间（ISO 或 YYYY-MM-DD）"),
    date_to: str | None = Query(None, description="结束时间（ISO 或 YYYY-MM-DD）"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """分页查询报警。支持按状态、级别、来源、点位与时间区间筛选。"""
    conds = []
    if status:
        conds.append(AlarmRecord.status == status)
    if level:
        conds.append(AlarmRecord.level == level)
    if source:
        conds.append(AlarmRecord.source == source)
    if camera_id:
        conds.append(AlarmRecord.camera_id == camera_id)
    start, end = _parse_dt(date_from), _parse_dt(date_to)
    if start:
        conds.append(AlarmRecord.created_at >= start)
    if end:
        conds.append(AlarmRecord.created_at <= end)

    total = (await db.execute(
        select(func.count()).select_from(AlarmRecord).where(*conds))).scalar() or 0
    rows = list((await db.execute(
        select(AlarmRecord).where(*conds)
        .order_by(AlarmRecord.id.desc()).offset(offset).limit(limit))).scalars().all())

    return AlarmPage(items=[AlarmOut.model_validate(r) for r in rows],
                     total=total, offset=offset, limit=limit)


@router.get("/summary")
async def alarm_summary(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """按状态与级别聚合，供仪表盘与值班看板使用。"""
    by_status = dict((await db.execute(
        select(AlarmRecord.status, func.count()).group_by(AlarmRecord.status))).all())
    by_level = dict((await db.execute(
        select(AlarmRecord.level, func.count()).group_by(AlarmRecord.level))).all())
    by_source = dict((await db.execute(
        select(AlarmRecord.source, func.count()).group_by(AlarmRecord.source))).all())
    by_feedback = dict((await db.execute(
        select(AlarmRecord.feedback_label, func.count())
        .where(AlarmRecord.feedback_label.isnot(None))
        .group_by(AlarmRecord.feedback_label))).all())
    return {"by_status": by_status, "by_level": by_level,
            "by_source": by_source, "by_feedback": by_feedback}


@router.get("/{aid}", response_model=AlarmOut)
async def get_alarm(aid: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    rec = await db.get(AlarmRecord, aid)
    if rec is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "报警不存在")
    return rec


@router.patch("/{aid}", response_model=AlarmOut)
async def update_alarm(
    aid: int,
    body: AlarmUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_handler),
):
    """推进处置状态、填写备注与误报反馈。"""
    rec = await db.get(AlarmRecord, aid)
    if rec is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "报警不存在")

    if body.status is not None:
        if body.status not in VALID_STATUS:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST,
                                f"状态不合法，可选：{'/'.join(VALID_STATUS)}")
        rec.status = body.status
        rec.handler_id = user.id
        if body.status in ("resolved", "ignored"):
            rec.resolved_at = datetime.now(timezone.utc)
    if body.note is not None:
        rec.note = body.note
    if body.feedback_label is not None:
        if body.feedback_label not in VALID_FEEDBACK:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST,
                                f"反馈标签不合法，可选：{'/'.join(VALID_FEEDBACK)}")
        rec.feedback_label = body.feedback_label
    if body.ignored_reason is not None:
        rec.ignored_reason = body.ignored_reason

    await db.commit()
    await db.refresh(rec)
    return rec


@router.post("/bulk/status")
async def bulk_update_status(
    ids: list[int],
    status: str = Query(..., description="目标状态"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_handler),
):
    """批量处置：值班人员一次处理多条同类报警。"""
    if status not in VALID_STATUS:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "状态不合法")
    if not ids:
        return {"updated": 0}
    rows = list((await db.execute(
        select(AlarmRecord).where(AlarmRecord.id.in_(ids[:200])))).scalars().all())
    now = datetime.now(timezone.utc)
    for rec in rows:
        rec.status = status
        rec.handler_id = user.id
        if status in ("resolved", "ignored"):
            rec.resolved_at = now
    await db.commit()
    return {"updated": len(rows)}
