"""报警编排引擎：把感知层的判定结果升级为事件与报警，并固化取证材料。

职责边界：
- 事件去重：同一轨迹的同类行为在合并窗口内只写一条事件，避免逐帧刷屏
- 报警裁定：是否构成报警由「行为性质 + 置信度 + 冷却窗口」共同决定
- 取证固化：报警时刻落截图 + 开启视频片段录制 + 外发通知
- 状态可追溯：报警进入 pending 状态，由值班人员推进状态机

冷却与去重状态存放在 SQLite（``state_store``）而非进程内字典，
这样多 worker / 多实例部署下行为一致，重启也不会重复报警。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.asr import SpeechSegment
from app.audio.keywords import KeywordHit, summarize
from app.core.config import behavior_meta, settings
from app.models import AlarmRecord, ChatLog, DetectionEvent, EvidenceFile, RuntimeState
from app.services import evidence as ev
from app.services import notify
from app.services.clips import clip_recorder
from app.services.state_store import state_store
from app.vision.engine import FrameResult

log = logging.getLogger("services.alarm")


# ---------------------------------------------------------------- 状态键
def _cooldown_key(camera_id: int | None, source: str) -> str:
    return f"cooldown:{camera_id or 0}:{source}"


def _dedup_key(camera_id: int | None, event_type: str, track_key: str) -> str:
    return f"dedup:{camera_id or 0}:{event_type}:{track_key}"


def _track_key(track_ids: list[int]) -> str:
    return ",".join(str(t) for t in sorted(track_ids)) if track_ids else "-"


async def _in_cooldown(camera_id: int | None, source: str) -> bool:
    last = await state_store.get_float(_cooldown_key(camera_id, source), 0.0)
    return (time.time() - last) < settings.ALARM_COOLDOWN_SEC


async def _mark_alarm(camera_id: int | None, source: str) -> None:
    await state_store.set(_cooldown_key(camera_id, source), str(time.time()),
                          ttl=settings.ALARM_STATE_TTL_SEC)


async def _is_duplicate(camera_id: int | None, event_type: str, track_ids: list[int]) -> bool:
    """同类同轨迹事件在合并窗口内视为重复。

    先判定再写入；只有确实要落库时才写时间戳，避免判定失败把窗口"占掉"。
    """
    key = _dedup_key(camera_id, event_type, _track_key(track_ids))
    last = await state_store.get_float(key, 0.0)
    if (time.time() - last) < settings.ALARM_MERGE_WINDOW_SEC:
        return True
    await state_store.set(key, str(time.time()), ttl=settings.ALARM_STATE_TTL_SEC)
    return False


def _bbox_str(bbox) -> str | None:
    if bbox is None:
        return None
    return ",".join(f"{float(v):.4f}" for v in bbox)


# ---------------------------------------------------------------- 视觉
async def process_frame(
    db: AsyncSession,
    camera_id: int | None,
    frame_bgr,
    result: FrameResult,
    operator_id: int | None = None,
    frame_time: float = 0.0,
) -> dict[str, Any] | None:
    """消费一帧的感知结果：落事件、裁定报警、固化取证、外发通知。"""
    triggered: dict[str, Any] | None = None

    for hit in result.behaviors:
        meta = behavior_meta(hit.event_type)
        is_bullying = bool(meta["is_bullying"])

        if not await _is_duplicate(camera_id, hit.event_type, hit.track_ids):
            db.add(DetectionEvent(
                camera_id=camera_id,
                event_type=hit.event_type,
                label=str(meta["label"]),
                confidence=float(hit.confidence),
                frame_time=round(float(frame_time), 2),
                bbox=_bbox_str(hit.bbox),
                track_ids=_track_key(hit.track_ids),
                detail=json.dumps(hit.detail, ensure_ascii=False),
                is_bullying=is_bullying,
                operator_id=operator_id,
            ))
            await db.commit()

        if triggered is not None:
            continue
        if not is_bullying and hit.event_type not in ("fall", "smoke"):
            continue  # 聚集等只记事件
        if hit.confidence < settings.ALARM_MIN_CONFIDENCE:
            continue
        if await _in_cooldown(camera_id, "video"):
            continue

        frame_path = ev.save_frame(frame_bgr, prefix=f"alarm_{hit.event_type}")
        detail_txt = "、".join(f"{k}={v}" for k, v in list(hit.detail.items())[:6])
        alarm = AlarmRecord(
            camera_id=camera_id,
            level=str(meta["level"]),
            reason=f"视觉行为检测：{meta['label']}（置信度 {hit.confidence:.2f}"
                   + (f"；{detail_txt}" if detail_txt else "") + "）",
            source="video",
            status="pending",
            evidence_paths=ev.register_evidence_paths([frame_path]),
            creator_id=operator_id,
        )
        db.add(alarm)
        await db.flush()
        db.add(EvidenceFile(alarm_id=alarm.id, kind="frame", path=frame_path, mime="image/jpeg"))
        await db.commit()
        await _mark_alarm(camera_id, "video")

        # 开启视频片段录制（预录来自环形缓冲，后延由后续帧补齐）
        clip_recorder.start(camera_id, alarm.id)

        triggered = {
            "alarm_id": alarm.id,
            "level": alarm.level,
            "reason": alarm.reason,
            "event_type": hit.event_type,
            "label": str(meta["label"]),
            "confidence": float(hit.confidence),
            "track_ids": hit.track_ids,
            "evidence": [frame_path],
            "created_at": alarm.created_at.isoformat() if alarm.created_at else None,
        }
        # 外发通知不阻塞检测主流程
        asyncio.create_task(notify.notify_alarm(triggered))

    return triggered


async def attach_clip(db: AsyncSession, alarm_id: int, clip_path: str) -> None:
    """把已生成的视频片段登记到报警与取证列表。"""
    alarm = await db.get(AlarmRecord, alarm_id)
    if alarm is None:
        return
    paths = ev.parse_evidence_paths(alarm.evidence_paths)
    if clip_path in paths:
        return
    paths.append(clip_path)
    alarm.evidence_paths = ev.register_evidence_paths(paths)
    db.add(EvidenceFile(alarm_id=alarm_id, kind="clip", path=clip_path, mime="video/mp4"))
    await db.commit()
    log.info("报警 #%s 已关联视频片段 %s", alarm_id, clip_path)


# ---------------------------------------------------------------- 语音
async def process_speech(
    db: AsyncSession,
    camera_id: int | None,
    segment: SpeechSegment,
    hits: list[KeywordHit],
    operator_id: int | None = None,
) -> dict[str, Any] | None:
    """消费一段语音转写：落库对话记录，命中关键词则裁定报警。

    ``segment.partial=True`` 的中间结果不参与报警，避免同一句话反复触发。
    """
    if segment.partial:
        return None

    summary = summarize(hits)
    level = summary["level"]
    alarm_id: int | None = None
    triggered: dict[str, Any] | None = None

    if level and not await _in_cooldown(camera_id, "audio"):
        keyword_txt = "、".join(summary["keywords"])
        path = ev.save_transcript(segment.text, prefix=f"speech_{int(time.time() * 1000)}")
        alarm = AlarmRecord(
            camera_id=camera_id,
            level=str(level),
            reason=f"语音关键词命中：{keyword_txt}｜“{segment.text}”",
            source="audio",
            status="pending",
            evidence_paths=ev.register_evidence_paths([path]),
            creator_id=operator_id,
        )
        db.add(alarm)
        await db.flush()
        alarm_id = alarm.id
        db.add(EvidenceFile(alarm_id=alarm.id, kind="transcript", path=path, mime="text/plain"))
        await db.commit()
        await _mark_alarm(camera_id, "audio")

        triggered = {
            "alarm_id": alarm.id,
            "level": alarm.level,
            "reason": alarm.reason,
            "source": "audio",
            "text": segment.text,
            "hit_keywords": summary["keywords"],
            "confidence": segment.confidence,
            "evidence": [path],
            "created_at": alarm.created_at.isoformat() if alarm.created_at else None,
        }
        asyncio.create_task(notify.notify_alarm(triggered))

    db.add(ChatLog(
        camera_id=camera_id,
        alarm_id=alarm_id,
        speaker=segment.speaker,
        text=segment.text,
        hit_keywords="、".join(summary["keywords"]) if summary["keywords"] else None,
        confidence=float(segment.confidence),
        start_time=float(segment.start),
        end_time=float(segment.end),
        is_final=True,
    ))
    await db.commit()
    return triggered


async def stats(db: AsyncSession) -> dict[str, Any]:
    """冷却/去重状态的运行态快照，便于观测。"""
    total = (await db.execute(select(func.count()).select_from(RuntimeState))).scalar() or 0
    active = (await db.execute(
        select(func.count()).select_from(RuntimeState)
        .where(RuntimeState.expires_at > time.time()))).scalar() or 0
    return {
        "state_entries": total,
        "active_entries": active,
        "cooldown_sec": settings.ALARM_COOLDOWN_SEC,
        "merge_window_sec": settings.ALARM_MERGE_WINDOW_SEC,
    }
