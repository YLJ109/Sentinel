"""事前预警：基于历史事件的时空聚集规律，输出点位与时段维度的风险画像。

与实时报警的职责分工：
- ``detect.py`` / ``alarm.py`` 回答「刚刚发生了什么」，属于事中与事后；
- 本模块回答「哪里、什么时候最容易出事」，属于事前。

校园场景下，把干预前移的价值高于事后取证 —— 这也是本项目从「事件检测」
走向「风险预测」的跃迁点。公开数据集（如 NWPU Campus）同样把「异常预测」
作为与「异常检测」并列的独立 benchmark，说明这是学界公认的下一步方向。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models import Camera, DetectionEvent, User

router = APIRouter(prefix="/api/risk", tags=["事前预警"])

# 各行为类型的风险权重。用于把「事件条数」换算成「风险分」——
# 一次人员聚集与一次单向欺凌不应被等同看待。
TYPE_WEIGHT: dict[str, float] = {
    "bullying": 1.0,
    "fight": 0.9,
    "argue": 0.4,
    "fall": 0.5,
    "smoke": 0.2,
    "crowd": 0.1,
}
# 时间衰减常数（天）：越久远的事件对「当前风险」的贡献越小。
# 取 14 天意味着两周前的事件权重衰减到约 37%，符合校园场景的观察周期。
DECAY_DAYS = 14.0


def _trend(recent: int, prev: int) -> str:
    """点位趋势。样本太少时不下结论，避免用一两条事件就宣称「上升」。"""
    if recent + prev < 3:
        return "stable"
    if recent > prev * 1.5:
        return "up"
    if recent * 1.5 < prev:
        return "down"
    return "stable"


@router.get("/forecast")
async def risk_forecast(
    days: int = Query(14, ge=1, le=90, description="回溯天数"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """风险画像：点位排行 + 高发时段 + 前后半程趋势。

    注意这里刻意**不使用实时推理**：全部基于已落库的历史事件统计，
    因此可以随时重算、不占用 GPU，也不会因为模型调整而改变历史结论。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now - timedelta(days=days)
    half = since + timedelta(days=days / 2)

    rows = (await db.execute(
        select(DetectionEvent.camera_id, DetectionEvent.event_type, DetectionEvent.created_at)
        .where(DetectionEvent.created_at >= since)
        .where(DetectionEvent.is_bullying.is_(True))
    )).all()

    cams = {c.id: c for c in (await db.execute(select(Camera))).scalars().all()}

    cam_score: dict[int, float] = defaultdict(float)
    cam_count: dict[int, int] = defaultdict(int)
    cam_hours: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    cam_recent: dict[int, int] = defaultdict(int)
    cam_prev: dict[int, int] = defaultdict(int)
    hour_total: dict[int, int] = defaultdict(int)

    for cam_id, etype, created in rows:
        if created is None:
            continue
        # 指数时间衰减：同一类型的事件，越近的权重越高
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        cam_score[cam_id] += TYPE_WEIGHT.get(etype, 0.3) * math.exp(-age_days / DECAY_DAYS)
        cam_count[cam_id] += 1
        cam_hours[cam_id][created.hour] += 1
        hour_total[created.hour] += 1
        if created >= half:
            cam_recent[cam_id] += 1
        else:
            cam_prev[cam_id] += 1

    cameras = []
    for cid, score in sorted(cam_score.items(), key=lambda kv: -kv[1]):
        c = cams.get(cid)
        peak = sorted(cam_hours[cid].items(), key=lambda kv: -kv[1])[:3]
        # 未关联点位是历史遗留数据（事件未绑定摄像头）。保留但明确标注，
        # 避免它以「点位 None」的形态占住风险榜首位，误导巡视安排。
        if cid is None:
            name = "未关联点位（历史数据）"
        else:
            name = c.name if c else f"点位 {cid}"
        cameras.append({
            "camera_id": cid,
            "name": name,
            "location": (c.location or "") if c else "",
            "risk_score": round(score, 3),
            "events": cam_count[cid],
            "peak_hours": [h for h, _ in peak],
            "trend": _trend(cam_recent[cid], cam_prev[cid]),
        })

    recent_total = sum(cam_recent.values())
    prev_total = sum(cam_prev.values())
    total = recent_total + prev_total
    return {
        "window_days": days,
        "total_events": len(rows),
        "cameras": cameras[:8],
        "hours": [{"hour": h, "events": hour_total.get(h, 0)} for h in range(24)],
        "trend": {
            "recent": recent_total,
            "previous": prev_total,
            "delta": round((recent_total - prev_total) / total, 3) if total else 0.0,
        },
    }
