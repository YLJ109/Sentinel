"""历史取证：检测事件、语音转写、报警关联回放、证据包导出。"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.core.timeutil import parse_dt_query
from app.models import AlarmRecord, ChatLog, DetectionEvent, User, VideoRecord
from app.schemas import (AlarmOut, ChatLogOut, ChatLogPage, DetectionEventOut, EventPage,
                         IdsIn, VideoPage, VideoRecordOut)

router = APIRouter(prefix="/api/history", tags=["历史取证"])

# 删除取证记录属高影响操作：管理员与值班操作员可用，观察员只读
_can_delete = require_role("admin", "operator")

# 时间参数解析统一走公共实现（原先本文件与 alarms.py 各复制了一份相同逻辑）
_parse_dt = parse_dt_query


@router.get("/events", response_model=EventPage)
async def list_events(
    camera_id: int | None = Query(None),
    event_type: str | None = Query(None),
    bullying_only: bool = Query(False),
    keyword: str | None = Query(None, description="按行为标签 / 判定特征模糊搜索"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conds = []
    if camera_id:
        conds.append(DetectionEvent.camera_id == camera_id)
    if event_type:
        conds.append(DetectionEvent.event_type == event_type)
    if bullying_only:
        conds.append(DetectionEvent.is_bullying.is_(True))
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        conds.append(or_(DetectionEvent.label.like(like),
                         DetectionEvent.event_type.like(like),
                         DetectionEvent.detail.like(like)))
    start, end = _parse_dt(date_from), _parse_dt(date_to)
    if start:
        conds.append(DetectionEvent.created_at >= start)
    if end:
        conds.append(DetectionEvent.created_at <= end)

    total = (await db.execute(select(func.count()).select_from(DetectionEvent).where(*conds))).scalar() or 0
    rows = list((await db.execute(
        select(DetectionEvent).where(*conds)
        .order_by(DetectionEvent.id.desc()).offset(offset).limit(limit))).scalars().all())
    return EventPage(items=[DetectionEventOut.model_validate(r) for r in rows],
                     total=total, offset=offset, limit=limit)


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(_can_delete),
):
    row = await db.get(DetectionEvent, event_id)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "检测事件不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": 1}


@router.post("/events/delete")
async def delete_events(
    body: IdsIn, db: AsyncSession = Depends(get_db), _: User = Depends(_can_delete),
):
    """批量删除检测事件（按 id 列表）。"""
    if not body.ids:
        return {"ok": True, "deleted": 0}
    rows = list((await db.execute(
        select(DetectionEvent).where(DetectionEvent.id.in_(body.ids)))).scalars().all())
    for r in rows:
        await db.delete(r)
    await db.commit()
    return {"ok": True, "deleted": len(rows)}


@router.get("/chatlogs", response_model=ChatLogPage)
async def list_chatlogs(
    alarm_id: int | None = Query(None),
    camera_id: int | None = Query(None),
    final_only: bool = Query(True),
    keyword: str | None = Query(None, description="按转写文本 / 命中关键词模糊搜索"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conds = []
    if alarm_id:
        conds.append(ChatLog.alarm_id == alarm_id)
    if camera_id:
        conds.append(ChatLog.camera_id == camera_id)
    if final_only:
        conds.append(ChatLog.is_final.is_(True))
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        conds.append(or_(ChatLog.text.like(like), ChatLog.hit_keywords.like(like)))
    start, end = _parse_dt(date_from), _parse_dt(date_to)
    if start:
        conds.append(ChatLog.created_at >= start)
    if end:
        conds.append(ChatLog.created_at <= end)

    total = (await db.execute(select(func.count()).select_from(ChatLog).where(*conds))).scalar() or 0
    rows = list((await db.execute(
        select(ChatLog).where(*conds)
        .order_by(ChatLog.id.desc()).offset(offset).limit(limit))).scalars().all())
    return ChatLogPage(items=[ChatLogOut.model_validate(r) for r in rows],
                       total=total, offset=offset, limit=limit)


@router.delete("/chatlogs/{log_id}")
async def delete_chatlog(
    log_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(_can_delete),
):
    row = await db.get(ChatLog, log_id)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "对话记录不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": 1}


@router.post("/chatlogs/delete")
async def delete_chatlogs(
    body: IdsIn, db: AsyncSession = Depends(get_db), _: User = Depends(_can_delete),
):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    rows = list((await db.execute(select(ChatLog).where(ChatLog.id.in_(body.ids)))).scalars().all())
    for r in rows:
        await db.delete(r)
    await db.commit()
    return {"ok": True, "deleted": len(rows)}


@router.get("/videos", response_model=VideoPage)
async def list_videos(
    status: str | None = Query(None),
    keyword: str | None = Query(None, description="按文件名模糊搜索"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conds = []
    if status:
        conds.append(VideoRecord.status == status)
    kw = (keyword or "").strip()
    if kw:
        conds.append(VideoRecord.filename.like(f"%{kw}%"))
    start, end = _parse_dt(date_from), _parse_dt(date_to)
    if start:
        conds.append(VideoRecord.created_at >= start)
    if end:
        conds.append(VideoRecord.created_at <= end)

    total = (await db.execute(select(func.count()).select_from(VideoRecord).where(*conds))).scalar() or 0
    rows = list((await db.execute(
        select(VideoRecord).where(*conds)
        .order_by(VideoRecord.id.desc()).offset(offset).limit(limit))).scalars().all())
    return VideoPage(items=[VideoRecordOut.model_validate(r) for r in rows],
                     total=total, offset=offset, limit=limit)


def _remove_media_file(rel: str | None) -> None:
    """删除记录时一并清掉磁盘上的原片 / 标注片，避免留存孤儿文件。"""
    if not rel:
        return
    root = settings.DATA_DIR.resolve()
    path = (root / str(rel).lstrip("/\\")).resolve()
    if root in path.parents and path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


@router.delete("/videos/{video_id}")
async def delete_video(
    video_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(_can_delete),
):
    row = await db.get(VideoRecord, video_id)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "视频记录不存在")
    paths = [row.original_path, row.processed_path]
    await db.delete(row)
    await db.commit()
    for p in paths:
        _remove_media_file(p)
    return {"ok": True, "deleted": 1}


@router.post("/videos/delete")
async def delete_videos(
    body: IdsIn, db: AsyncSession = Depends(get_db), _: User = Depends(_can_delete),
):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    rows = list((await db.execute(
        select(VideoRecord).where(VideoRecord.id.in_(body.ids)))).scalars().all())
    paths: list[str | None] = []
    for r in rows:
        paths += [r.original_path, r.processed_path]
        await db.delete(r)
    await db.commit()
    for p in paths:
        _remove_media_file(p)
    return {"ok": True, "deleted": len(rows)}


@router.get("/case/{alarm_id}")
async def case_detail(
    alarm_id: int,
    window_sec: int = Query(20, ge=5, le=180, description="报警时刻前后各取多长时间的关联证据"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """取证档案：报警本体 + 时间窗内的关联事件与对话转写 + 取证文件。

    报警之间不再通过外键硬关联事件，而是用「同点位 + 前后时间窗」聚合，
    这样一次冲突中的多个行为与语音会被完整还原到同一份档案里。
    """
    alarm = await db.get(AlarmRecord, alarm_id)
    if alarm is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "报警不存在")

    events: list[DetectionEvent] = []
    chats: list[ChatLog] = []

    if alarm.camera_id and alarm.created_at:
        center = alarm.created_at
        # 保持与 center 相同的时区语义，避免 naive/aware 混用比较报错
        start = center - timedelta(seconds=window_sec)
        end = center + timedelta(seconds=window_sec)

        events = list((await db.execute(
            select(DetectionEvent)
            .where(DetectionEvent.camera_id == alarm.camera_id)
            .where(DetectionEvent.created_at >= start)
            .where(DetectionEvent.created_at <= end)
            .order_by(DetectionEvent.id)
        )).scalars().all())

        chats = list((await db.execute(
            select(ChatLog)
            .where(ChatLog.camera_id == alarm.camera_id)
            .where(ChatLog.created_at >= start)
            .where(ChatLog.created_at <= end)
            .order_by(ChatLog.start_time)
        )).scalars().all())

    return {
        "alarm": AlarmOut.model_validate(alarm),
        "events": [DetectionEventOut.model_validate(e) for e in events],
        "chatlogs": [ChatLogOut.model_validate(c) for c in chats],
        "evidence": _parse(alarm.evidence_paths),
        "window_sec": window_sec,
    }


@router.get("/export/alarm/{alarm_id}")
async def export_alarm_bundle(
    alarm_id: int,
    window_sec: int = Query(30, ge=5, le=180),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把一条报警的全部取证材料打包为 zip，供归档、打印或报送上级。

    包含：报警信息与处置记录（manifest.json）、截图、视频片段、语音转写文本。
    """
    alarm = await db.get(AlarmRecord, alarm_id)
    if alarm is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "报警不存在")

    case = await case_detail(alarm_id, window_sec=window_sec, db=db, _=user)
    manifest = {
        "导出时间": datetime.now(timezone.utc).isoformat(),
        "导出人": user.username,
        "报警": json.loads(case["alarm"].model_dump_json()),
        "关联事件": [json.loads(e.model_dump_json()) for e in case["events"]],
        "关联对话": [json.loads(c.model_dump_json()) for c in case["chatlogs"]],
        "取证文件清单": case["evidence"],
    }

    buf = io.BytesIO()
    root = settings.DATA_DIR
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in case["evidence"]:
            path = (root / rel).resolve()
            if root.resolve() in path.parents and path.is_file():
                zf.write(path, arcname=rel.replace("/", "_"))
            else:
                zf.writestr(f"{rel}.missing.txt", "该取证文件已不存在（可能已被留存策略清理）")
    buf.seek(0)

    fname = f"alarm_{alarm_id}_evidence.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/feedback")
async def export_feedback(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """导出带反馈标签的报警样本，用于模型迭代与误报率统计。

    输出 JSON Lines：每行一条样本，含判定特征（detail）、点位、级别与人工反馈标签。
    """
    rows = list((await db.execute(
        select(AlarmRecord).where(AlarmRecord.feedback_label.isnot(None))
        .order_by(AlarmRecord.id))).scalars().all())

    # 一次性把全部报警时间窗内的候选事件取回来，再在内存里按点位就近匹配。
    # 原实现是"每条报警各查一次事件"，样本量一大就是典型 N+1（1000 条报警 = 1001 次查询）。
    events_by_cam: dict[int, list[DetectionEvent]] = {}
    dated = [a for a in rows if a.created_at and a.camera_id]
    if dated:
        lo = min(a.created_at for a in dated) - timedelta(seconds=30)
        hi = max(a.created_at for a in dated) + timedelta(seconds=30)
        evs = (await db.execute(
            select(DetectionEvent)
            .where(DetectionEvent.camera_id.in_({a.camera_id for a in dated}))
            .where(DetectionEvent.created_at >= lo)
            .where(DetectionEvent.created_at <= hi)
            .order_by(DetectionEvent.id))).scalars().all()
        for e in evs:
            events_by_cam.setdefault(e.camera_id, []).append(e)

    lines = []
    for a in rows:
        ev = None
        if a.created_at and a.camera_id:
            lo_a = a.created_at - timedelta(seconds=30)
            hi_a = a.created_at + timedelta(seconds=30)
            for cand in events_by_cam.get(a.camera_id, ()):
                if lo_a <= cand.created_at <= hi_a:
                    ev = cand
                    break
        detail = None
        if ev and ev.detail:
            try:
                detail = json.loads(ev.detail)
            except (TypeError, ValueError):
                detail = None
        lines.append(json.dumps({
            "alarm_id": a.id,
            "camera_id": a.camera_id,
            "level": a.level,
            "source": a.source,
            "reason": a.reason,
            "event_type": ev.event_type if ev else None,
            "confidence": ev.confidence if ev else None,
            "features": detail,
            "label": a.feedback_label,
            "comment": a.ignored_reason or a.note,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }, ensure_ascii=False))

    body = "\n".join(lines)
    return StreamingResponse(
        io.BytesIO(body.encode("utf-8")),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="feedback_samples.jsonl"'},
    )


def _parse(raw):
    from app.services import evidence as ev
    return ev.parse_evidence_paths(raw)
