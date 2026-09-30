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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.asr import SpeechSegment
from app.audio.keywords import KeywordHit, summarize
from app.core.config import behavior_meta, settings
from app.models import AlarmRecord, ChatLog, DetectionEvent, EvidenceFile, RuntimeState
from app.services import evidence as ev
from app.services import keyword_store
from app.services import notify
from app.services.clips import clip_recorder
from app.services.state_store import state_store
from app.vision.engine import FrameResult
from app.vision.privacy import is_masked

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


# ---------------------------------------------------------------- 离线（视频文件）去重
# 实时链路用墙上时钟做去重，但**离线视频分析跑得比实时快**（3 倍速以上），
# 用墙上时钟会把视频里相隔几分钟的两次真实事件合并成一次；反过来，
# 一次持续 30 秒的推搡在视频时间轴上本应被合并，用墙上时钟却会因为
# "处理只花了 10 秒"而拆成好几条。两边都是错的。
# 因此离线分析改用**视频时间轴**上的秒数做窗口判断，状态放进程内存
# （任务结束即清理，不需要跨进程共享）。
_offline_state: dict[str, float] = {}


def _offline_dup(key: str, t: float, window: float) -> bool:
    last = _offline_state.get(key)
    if last is not None and 0.0 <= (t - last) < window:
        return True
    _offline_state[key] = t
    return False


def clear_offline_state(video_id: int) -> None:
    """视频任务结束后回收其去重状态，避免长跑累积。"""
    prefix = f"v{video_id}:"
    for k in [k for k in _offline_state if k.startswith(prefix)]:
        _offline_state.pop(k, None)


def _bbox_str(bbox) -> str | None:
    if bbox is None:
        return None
    return ",".join(f"{float(v):.4f}" for v in bbox)


def _persons_of(result: FrameResult, track_ids: list[int]) -> tuple[list[dict], dict | None]:
    """从本帧轨迹里取出该行为涉及人员的已确认身份与情绪。

    只有通过轨迹级投票确认过的身份才会出现在 ``track.meta["person"]``，
    因此这里不会把"猜出来的名字"写进事件记录。
    """
    persons: list[dict] = []
    emotion: dict | None = None
    for tr in result.tracks:
        if track_ids and tr.tid not in track_ids:
            continue
        p = tr.meta.get("person")
        if p:
            persons.append(p)
        if emotion is None:
            e = tr.meta.get("emotion")
            if e:
                emotion = e
    return persons, emotion


# ---------------------------------------------------------------- 多模态证据融合
_LEVEL_ORDER = ("low", "medium", "high")


def _escalate(level: str) -> str:
    """把报警级别上调一档（双模态互证时使用）。已是最高级则原样返回。"""
    try:
        i = _LEVEL_ORDER.index(level)
    except ValueError:
        return level
    return _LEVEL_ORDER[min(len(_LEVEL_ORDER) - 1, i + 1)]


def _window_start(seconds: float) -> datetime:
    """关联时间窗的起点（naive UTC，与库内存储口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)


async def _recent_speech_keywords(db: AsyncSession, camera_id: int | None,
                                  window_sec: float) -> list[str]:
    """同点位最近若干秒内命中的语音关键词（去重）。"""
    rows = (await db.execute(
        select(ChatLog.hit_keywords)
        .where(ChatLog.camera_id == camera_id)
        .where(ChatLog.created_at >= _window_start(window_sec))
        .where(ChatLog.hit_keywords.isnot(None))
        .order_by(ChatLog.id.desc())
        .limit(20)
    )).scalars().all()
    words: list[str] = []
    for raw in rows:
        for w in str(raw).split("、"):
            w = w.strip()
            if w and w not in words:
                words.append(w)
    return words


async def _offline_recent_speech(db: AsyncSession, video_id: int, t: float,
                                 window_sec: float) -> list[str]:
    """离线视频：与**视频时间 t** 相近的语音关键词（去重）。

    与实时链路的关键差异：这里不能用 created_at（墙上时钟）比较 ——
    音频是前端边解码边推送的，其入库时刻与视频里的实际时刻完全脱钩，
    必须按 start_time（视频秒数）来对齐。
    """
    rows = (await db.execute(
        select(ChatLog.hit_keywords)
        .where(ChatLog.video_id == video_id)
        .where(ChatLog.hit_keywords.isnot(None))
        .where(ChatLog.start_time >= t - window_sec)
        .where(ChatLog.start_time <= t + window_sec)
        .order_by(ChatLog.id.desc())
        .limit(20)
    )).scalars().all()
    words: list[str] = []
    for raw in rows:
        for w in str(raw).split("、"):
            w = w.strip()
            if w and w not in words:
                words.append(w)
    return words


async def _recent_visual_events(db: AsyncSession, camera_id: int | None,
                                window_sec: float) -> list[str]:
    """同点位最近若干秒内的霸凌类视觉事件标签（去重）。"""
    rows = (await db.execute(
        select(DetectionEvent.label)
        .where(DetectionEvent.camera_id == camera_id)
        .where(DetectionEvent.created_at >= _window_start(window_sec))
        .where(DetectionEvent.is_bullying.is_(True))
        .order_by(DetectionEvent.id.desc())
        .limit(20)
    )).scalars().all()
    labels: list[str] = []
    for lb in rows:
        if lb and lb not in labels:
            labels.append(str(lb))
    return labels


# ---------------------------------------------------------------- 视觉
async def process_frame(
    db: AsyncSession,
    camera_id: int | None,
    frame_bgr,
    result: FrameResult,
    operator_id: int | None = None,
    frame_time: float = 0.0,
    video_id: int | None = None,
) -> dict[str, Any] | None:
    """消费一帧的感知结果：落事件、裁定报警、固化取证、外发通知。

    ``video_id`` 非空表示这是离线视频分析：事件会挂到视频任务上，
    且去重/冷却改用视频时间轴（详见 _offline_dup 的说明）。
    """
    triggered: dict[str, Any] | None = None
    offline = video_id is not None

    for hit in result.behaviors:
        # 隐私遮蔽：目标框中心落在遮蔽区内的，不产生事件、不落取证。
        # 放在最前面拦截 —— 一旦落库或截图，隐私就已经泄露，后面再过滤没有意义。
        if is_masked(hit.bbox, camera_id):
            continue

        meta = behavior_meta(hit.event_type)
        is_bullying = bool(meta["is_bullying"])

        track_key = _track_key(hit.track_ids)
        if offline:
            dup = _offline_dup(f"v{video_id}:ev:{hit.event_type}:{track_key}",
                               float(frame_time), settings.ALARM_MERGE_WINDOW_SEC)
        else:
            dup = await _is_duplicate(camera_id, hit.event_type, hit.track_ids)

        if not dup:
            # 离线视频把当时识别到的身份与情绪一并记进 detail：
            # 事件表本身不存人员字段（它属于人员模块），而这几项恰恰是
            # 事后复核时最有用的上下文（"是谁在什么情绪下被推"）。
            detail_obj = dict(hit.detail or {})
            if offline:
                persons, emotion = _persons_of(result, hit.track_ids)
                if persons:
                    detail_obj["persons"] = persons
                if emotion:
                    detail_obj["emotion"] = emotion
            db.add(DetectionEvent(
                camera_id=camera_id,
                video_id=video_id,
                event_type=hit.event_type,
                label=str(meta["label"]),
                confidence=float(hit.confidence),
                frame_time=round(float(frame_time), 2),
                bbox=_bbox_str(hit.bbox),
                track_ids=track_key,
                detail=json.dumps(detail_obj, ensure_ascii=False),
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
        if offline:
            if _offline_dup(f"v{video_id}:cd:video", float(frame_time), settings.ALARM_COOLDOWN_SEC):
                continue
        elif await _in_cooldown(camera_id, "video"):
            continue

        # ---- 多模态证据融合 ----
        # 若同点位近期的语音转写也命中了关键词，两条独立链路互为印证，
        # 报警可信度显著提高；对应的，级别只升不降。
        speech_words: list[str] = []
        if settings.FUSION_ENABLED:
            if offline:
                speech_words = await _offline_recent_speech(db, video_id, frame_time,
                                                            settings.FUSION_WINDOW_SEC)
            else:
                speech_words = await _recent_speech_keywords(db, camera_id, settings.FUSION_WINDOW_SEC)
        multimodal = bool(speech_words)

        conf = float(hit.confidence)
        level = str(meta["level"])
        if multimodal:
            conf = min(0.99, conf + settings.FUSION_BOOST)
            level = _escalate(level)

        frame_path = ev.save_frame(frame_bgr, prefix=f"alarm_{hit.event_type}")
        detail_txt = "、".join(f"{k}={v}" for k, v in list(hit.detail.items())[:6])
        reason = f"视觉行为检测：{meta['label']}（置信度 {conf:.2f}"
        if multimodal:
            reason += f"；语音同时命中关键词：{'、'.join(speech_words[:4])}"
        if detail_txt:
            reason += f"；{detail_txt}"
        reason += "）"

        alarm = AlarmRecord(
            camera_id=camera_id,
            level=level,
            reason=reason,
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
            "confidence": conf,
            "track_ids": hit.track_ids,
            "evidence": [frame_path],
            "multimodal": multimodal,
            "speech_keywords": speech_words[:4],
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
    video_id: int | None = None,
) -> dict[str, Any] | None:
    """消费一段语音转写：落库对话记录，命中关键词则裁定报警。

    ``segment.partial=True`` 的中间结果不参与报警，避免同一句话反复触发。

    ``video_id`` 非空表示这是**离线视频的音频轨**：转写会挂到视频任务上
    （start_time 即视频秒数），报警冷却改用视频时间轴判断。
    """
    if segment.partial:
        return None

    summary = summarize(hits)
    level = summary["level"]
    alarm_id: int | None = None
    triggered: dict[str, Any] | None = None
    offline = video_id is not None

    # 只有 alarm 档才触发报警。词表扩到 3000+ 后，"命中即报警"会让误报泛滥 ——
    # "垃圾""你妈"在正常对话里也会出现。warn 档只记入对话流并做警告提示，
    # highlight 档仅高亮，两者都不生成报警记录，由前端分级标色呈现。
    in_cd = (_offline_dup(f"v{video_id}:cd:audio", float(segment.start), settings.ALARM_COOLDOWN_SEC)
             if offline else await _in_cooldown(camera_id, "audio"))
    if level == "alarm" and not in_cd:
        # 档位 → 报警级别：能走到这里的只有 alarm 档，因此固定为最高级别
        alarm_level = "high"

        # ---- 多模态证据融合（反向：语音报警时回查同期视觉证据）----
        visual_labels: list[str] = []
        if settings.FUSION_ENABLED:
            visual_labels = await _recent_visual_events(db, camera_id, settings.FUSION_WINDOW_SEC)
        multimodal = bool(visual_labels)
        if multimodal:
            alarm_level = _escalate(alarm_level)

        keyword_txt = "、".join(summary["keywords"])
        reason = f"语音关键词命中：{keyword_txt}｜“{segment.text}”"
        if offline:
            # 离线视频必须把时间点写进理由：事后看报警列表时，
            # "出现在 03:12" 是唯一能定位到原始画面的线索。
            reason = f"【视频 0{int(segment.start) // 60}:{int(segment.start) % 60:02d}】" + reason
        if multimodal:
            reason += f"；同期视觉亦检出：{'、'.join(visual_labels[:3])}"

        path = ev.save_transcript(segment.text, prefix=f"speech_{int(time.time() * 1000)}")
        alarm = AlarmRecord(
            camera_id=camera_id,
            level=alarm_level,
            reason=reason,
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
        # 离线分析不写实时冷却状态：否则一段视频里的语音报警会把该点位
        # 之后几分钟的真实语音报警全部“冷却”掉（用户完全无法理解为什么没报警）
        if not offline:
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
            "multimodal": multimodal,
            "visual_labels": visual_labels[:3],
            "created_at": alarm.created_at.isoformat() if alarm.created_at else None,
        }
        asyncio.create_task(notify.notify_alarm(triggered))

    db.add(ChatLog(
        camera_id=camera_id,
        video_id=video_id,
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

    # 命中次数统计：只统计 alarm 与 warn 两档。
    # highlight 档词条数以千计且大多只是复核线索，把它们计入会让"命中排行"
    # 失去参考价值 —— 排行榜应该反映"哪些词真的在起作用"。
    tracked = [*summary["bullying"], *summary["alarm"]]
    if tracked:
        try:
            await keyword_store.bump_hits(db, tracked)
        except Exception as e:  # noqa: BLE001 —— 统计失败不应影响报警主链路
            log.warning("关键词命中统计写入失败：%s", e)

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
