"""报警记录管理（取证与处置核心）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import BEHAVIOR_LABELS
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.core.timeutil import parse_dt_query
from app.models import AlarmRecord, DetectionEvent, User
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


# 复核建议对应的可调参数：把统计结论直接映射到参数名，
# 否则"误报率高"只是一句结论，使用者不知道该改什么。
_THRESHOLD_HINT: dict[str, str] = {
    "bullying": "BULLY_SCORE_THRESHOLD（提高可减少误报）",
    "fight": "FIGHT_WRIST_SPEED 或 FIGHT_MIN_FRAMES（提高可减少误报）",
    "argue": "ARGUE_MIN_FRAMES（提高）或 ARGUE_DIST_RATIO（收紧）",
    "smoke": "SMOKE_MIN_FRAMES（提高）或 SMOKE_HAND_HEAD_RATIO（收紧）",
    "fall": "FALL_MIN_FRAMES（提高可减少误报）",
    "crowd": "CROWD_MIN_PEOPLE（提高可减少误报）",
}

# 由中文标签反查事件类型：历史报警没有关联 event_id，只能从 reason 文本里解析
_LABEL_TO_TYPE: dict[str, str] = {str(v["label"]): k for k, v in BEHAVIOR_LABELS.items()}


# 路径刻意用两段（/review/summary）而不是 /review-summary：
# 单段会被前面定义的 GET /{alarm_id} 抢先匹配，导致 alarm_id 转换失败返回 422。
@router.get("/review/summary")
async def review_summary(
    days: int = Query(30, ge=1, le=365, description="回溯天数"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """复核闭环：把人工复核结果汇总成可行动的结论。

    闭环的价值在「报警 → 人工复核 → 统计 → 反向指导阈值调整」。
    没有这一步，误报率永远是个说不清的数字，判定阈值也无从迭代 ——
    而这恰恰是评审一定会追问的「你怎么知道系统准不准」的唯一诚实答案。
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = (await db.execute(
        select(AlarmRecord.feedback_label, AlarmRecord.status,
               DetectionEvent.event_type, AlarmRecord.reason)
        .outerjoin(DetectionEvent, AlarmRecord.event_id == DetectionEvent.id)
        .where(AlarmRecord.created_at >= since)
    )).all()

    # total 必须取实际返回的报警条数：漏掉这一步会让它恒为 0，
    # 于是 review_rate 也算成 0，整个复核率指标失去意义
    total = len(rows)
    reviewed = confirmed = false_pos = other = 0
    per_type: dict[str, dict[str, int]] = {}

    for feedback, status, etype, reason in rows:
        if not etype and reason:
            for label, t in _LABEL_TO_TYPE.items():
                if label in reason:
                    etype = t
                    break
        etype = etype or "unknown"

        bucket = per_type.setdefault(etype, {"total": 0, "reviewed": 0,
                                             "confirmed": 0, "false_positive": 0, "other": 0})
        bucket["total"] += 1
        if status in ("resolved", "ignored") or feedback:
            bucket["reviewed"] += 1
            reviewed += 1
        if feedback == "confirmed":
            bucket["confirmed"] += 1
            confirmed += 1
        elif feedback == "false_positive":
            bucket["false_positive"] += 1
            false_pos += 1
        elif feedback:
            bucket["other"] += 1
            other += 1

    by_type = []
    for etype, b in sorted(per_type.items(), key=lambda kv: -kv[1]["total"]):
        label = str(BEHAVIOR_LABELS.get(etype, {}).get("label", etype))
        judged = b["confirmed"] + b["false_positive"]
        rate = round(b["confirmed"] / judged, 3) if judged else None
        advice = None
        # 样本少于 5 条不下结论，避免用一两条复核就调整全局阈值
        if judged >= 5 and rate is not None:
            if rate < 0.5:
                advice = (f"确认率仅 {rate:.0%}，误报偏多；"
                          f"建议调整 {_THRESHOLD_HINT.get(etype, '判定阈值')}")
            elif rate >= 0.9:
                advice = f"确认率高达 {rate:.0%}，可适当放宽阈值以提高召回"
        by_type.append({
            "event_type": etype, "label": label,
            "total": b["total"], "reviewed": b["reviewed"],
            "confirmed": b["confirmed"], "false_positive": b["false_positive"],
            "confirm_rate": rate, "advice": advice,
        })

    return {
        "window_days": days,
        "total": total,
        "reviewed": reviewed,
        "review_rate": round(reviewed / total, 3) if total else 0.0,
        "confirmed": confirmed,
        "false_positive": false_pos,
        "other": other,
        "by_type": by_type,
    }
