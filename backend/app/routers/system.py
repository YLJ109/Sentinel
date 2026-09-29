"""系统运行时状态：推理设备、模型、能力就绪情况、运行参数与日志。

供控制台底部状态栏展示"哪些能力已就绪"，也供运维定位权重缺失、CUDA 不可用、
云端语音未配凭据等问题；日志接口让异常在界面上就能看到，不必登录服务器。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.asr import asr
from app.audio.keywords import build_dictionary
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.core.logbuffer import log_buffer
from app.core.runtime_config import ALLOWED_DEVICES, CAPABILITY_KEYS, runtime_config
from app.models import AlarmRecord, Camera, DetectionEvent, User
from app.schemas import CapabilityToggle, DeviceSwitch, LogQueryOut, SystemModelsOut
from app.services import alarm as alarm_svc
from app.vision.engine import engine
from app.vision.face import face_detector
from app.vision.registry import registry

router = APIRouter(prefix="/api/system", tags=["系统"])
log = logging.getLogger("routers.system")

# 人脸检测后端的中文说明（供状态栏 tooltip 显示）
FACE_BACKEND_ZH = {
    "yolo": "YOLO 人脸模型",
    "haar": "OpenCV 级联（可换 YOLO 权重提升召回）",
}


# ---------------------------------------------------------------- 能力就绪
def _capabilities() -> list[dict]:
    """汇总各检测能力的就绪状态，供底部状态栏逐项显示。"""
    st = registry.status()
    models = st.get("models") or {}

    def pick(kind: str) -> dict | None:
        return next((v for v in models.values() if v.get("kind") == kind), None)

    pose, detect, behavior = pick("pose"), pick("detect"), pick("behavior")
    pose_ok = bool(pose and pose.get("loaded"))
    detect_ok = bool(detect and detect.get("loaded"))
    person_ok = pose_ok or detect_ok
    behavior_ok = bool(behavior and behavior.get("loaded"))

    # 人脸检测：YOLO 权重 / OpenCV 级联 / 不可用
    face_st = face_detector.status()
    face_ok = bool(face_st.get("ready"))

    def detail(*parts: str | None) -> str:
        return " · ".join([p for p in parts if p])

    caps: list[dict] = [
        {
            "key": "person", "label": "人体检测", "group": "vision",
            "ready": person_ok, "degraded": False,
            "detail": detail(pose["weights"] if pose_ok else (detect or {}).get("weights"),
                             "姿态模型" if pose_ok else ("检测模型" if detect_ok else None)),
        },
        {
            "key": "pose", "label": "人体骨架", "group": "vision",
            "ready": pose_ok and settings.ENABLE_POSE,
            "degraded": not settings.ENABLE_POSE,
            "detail": "17 关键点" if (pose_ok and settings.ENABLE_POSE)
                      else ("已在配置中关闭" if not settings.ENABLE_POSE else "姿态模型未加载"),
        },
        {
            "key": "face", "label": "人脸检测", "group": "vision",
            "ready": face_ok, "degraded": face_st.get("backend") == "haar",
            "detail": FACE_BACKEND_ZH.get(str(face_st.get("backend")), str(face_st.get("error") or "不可用")),
        },
        {
            "key": "fall", "label": "跌倒检测", "group": "behavior",
            "ready": pose_ok, "degraded": False,
            "detail": detail("姿态几何判定", "连续 %d 帧确认" % settings.FALL_MIN_FRAMES) if pose_ok
                      else "依赖人体骨架，暂不可用",
        },
        {
            "key": "fight", "label": "打架检测", "group": "behavior",
            "ready": person_ok, "degraded": False,
            "detail": detail("双人距离+挥臂速度+抬手姿态",
                             "接入专项模型" if behavior_ok else None) if person_ok else "依赖人体检测",
        },
        {
            "key": "argue", "label": "吵架检测", "group": "behavior",
            "ready": person_ok, "degraded": False,
            "detail": detail("持续贴近+无剧烈动作", "连续 %d 帧确认" % settings.ARGUE_MIN_FRAMES)
                      if person_ok else "依赖人体检测",
        },
        {
            "key": "smoke", "label": "抽烟检测", "group": "behavior",
            "ready": person_ok, "degraded": not behavior_ok,
            "detail": "专项模型" if behavior_ok else "姿态代理特征（建议训练专用模型提升准确率）",
        },
        {
            "key": "crowd", "label": "聚集检测", "group": "behavior",
            "ready": person_ok, "degraded": False,
            "detail": f"人数 ≥ {settings.CROWD_MIN_PEOPLE}" if person_ok else "依赖人体检测",
        },
    ]

    speech = asr.status()
    caps.append({
        "key": "speech", "label": "语音识别", "group": "audio",
        "ready": bool(speech.get("ready")), "degraded": False,
        "detail": speech.get("error") or str(speech.get("engine") or ""),
    })

    kw = build_dictionary()
    caps.append({
        "key": "keyword", "label": "关键词检测", "group": "audio",
        "ready": len(kw) > 0, "degraded": False,
        "detail": f"词表 {len(kw)} 条 · 编辑距离容错",
    })

    # 运行时开关：状态栏上每一项都可以点开关，关掉后引擎不再做对应计算
    for c in caps:
        c["enabled"] = runtime_config.capability(c["key"])
    return caps


@router.get("/status")
async def system_status(request: Request, db: AsyncSession = Depends(get_db),
                        _: User = Depends(get_current_user)):
    """底部状态栏数据源：设备、能力就绪、运行态、待处置报警。"""
    st = registry.status()
    info = st.get("device_info") or {}
    caps = _capabilities()

    pending = (await db.execute(
        select(func.count()).select_from(AlarmRecord)
        .where(AlarmRecord.status == "pending"))).scalar() or 0
    cameras_total = (await db.execute(select(func.count()).select_from(Camera))).scalar() or 0
    cameras_on = (await db.execute(
        select(func.count()).select_from(Camera).where(Camera.enabled.is_(True)))).scalar() or 0

    # 今日统计：顶部信息模块用（比"能力就绪"更偏业务视角，与底部状态栏互补）
    #
    # 库内 created_at 统一按 UTC 存 naive 值，而"今日"要按站点本地时区切分。
    # 之前直接用本地 0 点（带本地时区语义的 naive 值）去和 UTC 存的 naive 值比较，
    # 相当于整体偏移了一个时区：本地 00:00~08:00 产生的数据会被判成"昨天"，
    # 实测本地凌晨时段今日事件恒为 0。正确做法是先取本地 0 点，再换算成 UTC 后比较。
    local_tz = datetime.now().astimezone().tzinfo
    day_start = (datetime.now(local_tz)
                 .replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(timezone.utc).replace(tzinfo=None))
    today_events = (await db.execute(
        select(func.count()).select_from(DetectionEvent)
        .where(DetectionEvent.created_at >= day_start))).scalar() or 0
    today_alarms = (await db.execute(
        select(func.count()).select_from(AlarmRecord)
        .where(AlarmRecord.created_at >= day_start))).scalar() or 0

    started = getattr(request.app.state, "started_at", time.time())
    ready_count = sum(1 for c in caps if c["ready"])
    enabled_count = sum(1 for c in caps if c["enabled"])
    return {
        "device": {
            "device": st.get("device"),
            "half": st.get("half"),
            "imgsz": st.get("imgsz"),
            "cuda_available": bool(info.get("cuda_available")),
            "gpu": info.get("gpu"),
            "gpu_memory_gb": info.get("gpu_memory_gb"),
            "torch": info.get("torch"),
            # 供状态栏做 CPU / CUDA 切换：当前选择 + 可选值
            "selected": runtime_config.device,
            "options": [d for d in ALLOWED_DEVICES
                        if d != "cuda:1" or settings.DEVICE == "cuda:1"],
        },
        "capabilities": caps,
        "summary": {"ready": ready_count, "total": len(caps), "enabled": enabled_count},
        "runtime": {
            "uptime_sec": int(time.time() - started),
            "pipelines": engine.pipelines_status(),
            "infer_fps_limit": settings.INFER_FPS_LIMIT,
            "motion_gate": settings.MOTION_GATE_ENABLED,
        },
        "cameras": {"total": cameras_total, "enabled": cameras_on},
        "alarms": {"pending": pending, "today": today_alarms},
        "events": {"today": today_events},
        "logs": {"error_count": log_buffer.error_count, "warning_count": log_buffer.warning_count},
    }


# ---------------------------------------------------------------- 运行时开关
@router.put("/capabilities/{key}")
async def set_capability(
    key: str,
    body: CapabilityToggle,
    _: User = Depends(require_role("admin", "operator")),
):
    """开关某项检测能力（状态栏点击）。开关落库，重启后保持。"""
    if key not in CAPABILITY_KEYS:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"未知的检测能力：{key}")
    await runtime_config.set_capability(key, body.enabled)
    label = CAPABILITY_KEYS[key][1]
    log.info("检测能力已%s：%s(%s)", "开启" if body.enabled else "关闭", label, key)
    return {
        "ok": True,
        "key": key,
        "label": label,
        "enabled": body.enabled,
        "capabilities": runtime_config.capabilities,
    }


@router.put("/device")
async def set_device(body: DeviceSwitch, _: User = Depends(require_role("admin"))):
    """热切换推理设备（CPU / CUDA）。

    模型与设备绑定，切换会丢弃并重新加载权重 + 重新预热，因此有几秒不可用；
    放在线程里执行，避免阻塞事件循环。
    """
    target = (body.device or "auto").strip().lower()
    if target not in ALLOWED_DEVICES:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST,
                            f"不支持的推理设备：{target}（可选 {'/'.join(ALLOWED_DEVICES)}）")
    if target.startswith("cuda") and not (registry.status().get("device_info") or {}).get("cuda_available"):
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "当前环境未检测到可用的 CUDA 设备")

    await runtime_config.set_device(target)
    info = await asyncio.to_thread(registry.switch_device, runtime_config.device)
    log.info("推理设备切换完成：%s", info)
    return {"ok": True, "requested": target, **info}


# ---------------------------------------------------------------- 日志
@router.get("/logs", response_model=LogQueryOut)
async def get_logs(
    level: str | None = Query(None, description="ERROR / WARN / INFO，留空为全部"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    since_id: int = Query(0, ge=0, description="仅返回该 id 之后的日志，用于增量拉取"),
    _: User = Depends(get_current_user),
):
    return log_buffer.snapshot(level=level, offset=offset, limit=limit, since_id=since_id)


@router.post("/logs/client")
async def push_client_log(payload: dict):
    """接收前端异常（window.onerror / Vue errorHandler），统一归集到同一日志流。

    该接口不要求鉴权：前端崩溃往往发生在登录态异常时，若强制鉴权会导致
    最需要排查的那类错误根本传不上来；写入内容仅作展示，不做任何执行。
    """
    message = str(payload.get("message") or "")[:2000]
    if not message:
        return {"ok": False}
    log_buffer.push_client(
        level=str(payload.get("level") or "error"),
        message=message,
        source=str(payload.get("source") or "frontend")[:64],
        traceback=(str(payload.get("stack"))[:4000] if payload.get("stack") else None),
    )
    return {"ok": True}


@router.post("/logs/clear")
async def clear_logs(_: User = Depends(require_role("admin"))):
    log_buffer.clear()
    return {"ok": True}


# ---------------------------------------------------------------- 模型与配置
@router.get("/models", response_model=SystemModelsOut)
async def model_status(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    status = registry.status()
    return SystemModelsOut(
        device=status["device"],
        half=status["half"],
        imgsz=status["imgsz"],
        device_info=status["device_info"],
        models=status["models"],
        speech=asr.status(),
        alarm_runtime=await alarm_svc.stats(db),
        pipelines=engine.pipelines_status(),
    )


@router.post("/retention/run")
async def run_retention(days: int | None = None, _: User = Depends(require_role("admin"))):
    """手动触发一次留存清理（运维用；日常由后台周期任务执行）。"""
    from app.services.retention import purge_once

    return await purge_once(days)


@router.get("/config")
async def get_runtime_config(_: User = Depends(get_current_user)):
    """对外暴露非敏感的运行参数，便于前端展示与现场标定对照。

    注意：函数名不能叫 ``runtime_config``，否则会遮蔽同名导入的运行时配置单例。
    """
    return {
        "env": settings.ENV,
        "infer_fps_limit": settings.INFER_FPS_LIMIT,
        "enable_pose": settings.ENABLE_POSE,
        "enable_face": settings.ENABLE_FACE,
        "face": face_detector.status(),
        "runtime_switches": {
            "device": runtime_config.device,
            "device_options": list(ALLOWED_DEVICES),
            "disabled_capabilities": [k for k, v in runtime_config.capabilities.items() if not v],
        },
        "detect_conf": settings.DETECT_CONF,
        "motion_gate": {
            "enabled": settings.MOTION_GATE_ENABLED,
            "diff_threshold": settings.MOTION_DIFF_THRESHOLD,
            "force_interval_sec": settings.MOTION_GATE_FORCE_INTERVAL_SEC,
        },
        "video_worker_concurrency": settings.VIDEO_WORKER_CONCURRENCY,
        "alarm_min_confidence": settings.ALARM_MIN_CONFIDENCE,
        "alarm_cooldown_sec": settings.ALARM_COOLDOWN_SEC,
        "alarm_merge_window_sec": settings.ALARM_MERGE_WINDOW_SEC,
        "evidence_keep_days": settings.EVIDENCE_KEEP_DAYS,
        "clip": {"pre_seconds": settings.CLIP_PRE_SECONDS, "post_seconds": settings.CLIP_POST_SECONDS},
        "notify": {"enabled": bool(settings.NOTIFY_WEBHOOK_URL), "min_level": settings.NOTIFY_MIN_LEVEL},
        "thresholds": {
            "fall_aspect_ratio": settings.FALL_ASPECT_RATIO,
            "fall_torso_angle": settings.FALL_TORSO_ANGLE,
            "fall_min_frames": settings.FALL_MIN_FRAMES,
            "fight_dist_ratio": settings.FIGHT_DIST_RATIO,
            "fight_wrist_speed": settings.FIGHT_WRIST_SPEED,
            "fight_min_frames": settings.FIGHT_MIN_FRAMES,
            "argue_dist_ratio": settings.ARGUE_DIST_RATIO,
            "argue_min_frames": settings.ARGUE_MIN_FRAMES,
            "smoke_hand_head_ratio": settings.SMOKE_HAND_HEAD_RATIO,
            "smoke_min_frames": settings.SMOKE_MIN_FRAMES,
            "crowd_min_people": settings.CROWD_MIN_PEOPLE,
        },
    }
