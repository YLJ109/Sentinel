"""实时感知接口：视频帧检测、音频流识别。

鉴权方式：WebSocket 无法携带自定义请求头，为避免把 JWT 放进 URL 查询参数
（会进入 Nginx/uvicorn 访问日志与浏览器历史），这里改用标准子协议字段传递：
浏览器侧 ``new WebSocket(url, ['cab.v1', token])``，服务端从
``Sec-WebSocket-Protocol`` 中取出令牌并回选 ``cab.v1``。

- ``POST /api/detect/frame``：HTTP 兜底单帧检测（便于调试与低帧率场景）
- ``WS /api/ws/detect``：浏览器摄像头帧（base64 JPEG）实时检测
- ``WS /api/ws/audio``：浏览器麦克风 PCM16/16kHz 原生音频流 → ASR Provider
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.asr import asr
from app.audio.keywords import scan, summarize
from app.core.config import behavior_meta, settings
from app.core.db import SessionLocal, get_db
from app.core.deps import get_current_user
from app.core.runtime_config import runtime_config
from app.core.security import MEDIA_SCOPE, decode_access_token
from app.models import User
from app.schemas import BehaviorBox, DetectionBox, FrameDetectRequest, FrameDetectResponse
from app.services import alarm as alarm_svc
from app.services.clips import clip_recorder
from app.services.image_io import b64_to_image
from app.vision.engine import FrameResult, engine

log = logging.getLogger("routers.detect")
router = APIRouter(tags=["实时感知"])

# WebSocket 子协议名：客户端需在子协议列表中同时给出协议名与令牌
WS_PROTOCOL = "cab.v1"


# ---------------------------------------------------------------- WS 鉴权
def _ws_token(websocket: WebSocket) -> str | None:
    """从子协议列表中取令牌：['cab.v1', '<jwt>']。"""
    protos = list(websocket.scope.get("subprotocols") or [])
    if not protos:
        return None
    if protos[0] == WS_PROTOCOL:
        return protos[1] if len(protos) > 1 else None
    # 兼容只传令牌的客户端
    return protos[0] if len(protos) == 1 else None


async def _accept(websocket: WebSocket) -> None:
    protos = websocket.scope.get("subprotocols") or []
    if WS_PROTOCOL in protos:
        await websocket.accept(subprotocol=WS_PROTOCOL)
    else:
        await websocket.accept()


# ---------------------------------------------------------------- 序列化
def _tracks_to_boxes(result: FrameResult) -> list[dict]:
    out: list[dict] = []
    for tr in result.tracks:
        if tr.misses > 0:
            continue
        out.append({
            "track_id": tr.tid,
            "event_type": "person",
            "label": "人员",
            "confidence": round(float(tr.conf), 3),
            "bbox": [round(float(v), 4) for v in tr.bbox],
            "is_bullying": False,
            "kpts": None if tr.kpts is None else [[round(float(a), 4), round(float(b), 4), round(float(c), 2)]
                                                  for a, b, c in tr.kpts],
            # 身份与情绪只在"轨迹级确认"之后才存在，未确认时为 None，
            # 前端据此显示"未识别人员"，绝不猜测
            "person": tr.meta.get("person"),
            "emotion": tr.meta.get("emotion"),
        })
    return out


def _behaviors_to_boxes(result: FrameResult) -> list[dict]:
    return [
        {
            "event_type": h.event_type,
            "label": str(behavior_meta(h.event_type)["label"]),
            "confidence": round(float(h.confidence), 3),
            "track_ids": list(h.track_ids),
            "bbox": None if h.bbox is None else [round(float(v), 4) for v in h.bbox],
            "is_bullying": bool(behavior_meta(h.event_type)["is_bullying"]),
            # 判定特征保留完整：欺凌/打架的区分依据（互动对称性四项指标）
            # 需要一并透出，前端可解释面板与取证详情都依赖它
            "detail": {k: v for k, v in list(h.detail.items())[:14]},
        }
        for h in result.behaviors
    ]


# ---------------------------------------------------------------- HTTP 单帧
@router.post("/api/detect/frame", response_model=FrameDetectResponse)
async def detect_frame(
    body: FrameDetectRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    img = b64_to_image(body.image_b64)
    if img is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图像解码失败")

    result = engine.infer(img, camera_id=body.camera_id,
                          timestamp=body.timestamp or time.time(), force=True)
    triggered = await alarm_svc.process_frame(db, body.camera_id, img, result, operator_id=user.id)

    return FrameDetectResponse(
        boxes=[DetectionBox(**b) for b in _tracks_to_boxes(result)],
        behaviors=[BehaviorBox(**b) for b in _behaviors_to_boxes(result)],
        faces=result.faces,
        people=result.people,
        infer_ms=result.infer_ms,
        alarm=triggered,
        server_time=time.time(),
    )


# ---------------------------------------------------------------- 视频帧 WS
@router.websocket("/api/ws/detect")
async def ws_detect(websocket: WebSocket):
    # 拒绝取证媒体令牌：它只用于读取媒体，不应能开启检测/音频通道
    user_id = decode_access_token(_ws_token(websocket) or "", forbid_scopes=(MEDIA_SCOPE,))
    if user_id is None:
        await websocket.close(code=4401)
        return
    await _accept(websocket)
    operator_id = int(user_id)

    try:
        while True:
            msg = await websocket.receive_json()
            camera_id = msg.get("camera_id")
            img = b64_to_image(msg.get("image_b64", ""))
            if img is None:
                await websocket.send_json({"error": "bad_image"})
                continue

            # 一律使用服务端时间戳。客户端时钟可能被挂起/回拨（页面切后台、系统改时间），
            # 一旦 t 小于上次推理时间，限频判断 (t - last_infer_at) < min_interval 会恒成立，
            # 整路检测直接"假死"且不报任何错。客户端时延观测量改由响应里的 server_time 提供。
            ts = time.time()

            # 1) 送检（同一路多观看者共享限频与结果，不会重复推理）
            result = await asyncio.to_thread(engine.infer, img, camera_id, ts, False)

            # 2) 预录环形缓冲：报警时用于回溯"事发前"的画面
            #    剪辑内部自持单调时钟，这里不再传时间戳，避免两套时基混用
            finished = await asyncio.to_thread(clip_recorder.push, camera_id, img)

            async with SessionLocal() as db:
                triggered = await alarm_svc.process_frame(
                    db, camera_id, img, result, operator_id=operator_id
                )
                # 3) 剪辑完成后登记到对应报警
                if finished:
                    await alarm_svc.attach_clip(db, finished[0], finished[1])

            await websocket.send_json({
                "boxes": _tracks_to_boxes(result),
                "behaviors": _behaviors_to_boxes(result),
                "faces": result.faces,
                "people": result.people,
                "infer_ms": result.infer_ms,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
                "motion": result.motion,
                "alarm": triggered,
                "server_time": time.time(),
            })
    except WebSocketDisconnect:
        return
    except Exception as e:  # 单路异常不应影响其它连接
        log.exception("检测 WS 异常：%s", e)
        return


# ---------------------------------------------------------------- 音频 WS
@router.websocket("/api/ws/audio")
async def ws_audio(websocket: WebSocket):
    """浏览器麦克风 PCM16/16kHz 单声道 → ASR Provider（云端 NLS / 本地 Vosk）。

    上行：二进制帧为音频；文本帧为控制指令（设置 camera_id / flush）。
    下行：``type`` 为 ``partial``（中间结果，仅回显）或 ``final``（成句，参与报警）。
    """
    user_id = decode_access_token(_ws_token(websocket) or "", forbid_scopes=(MEDIA_SCOPE,))
    if user_id is None:
        await websocket.close(code=4401)
        return
    await _accept(websocket)

    if not settings.SPEECH_ENABLED or not runtime_config.capability("speech"):
        await websocket.send_json({"type": "error", "error": "speech_disabled",
                                   "detail": "语音识别已关闭（可在底部状态栏重新开启，"
                                             "或检查 CAB_SPEECH_ENABLED）"})
        await websocket.close(code=1011)
        return

    if not asr.ready:
        try:
            await asyncio.wait_for(asr.prepare(), timeout=15)
        except Exception as e:
            await websocket.send_json({"type": "error", "error": "asr_unavailable", "detail": str(e)})
            await websocket.close(code=1011)
            return

    session = asr.open_session()
    camera_id: int | None = None
    # 离线视频模式：前端把视频音频轨解码后按同样协议推上来，
    # 这里只多记一个 video_id，转写与报警就都挂到该视频任务上。
    video_id: int | None = None

    async def emit(segment) -> None:
        hits = scan(segment.text) if runtime_config.capability("keyword") else []
        triggered = None
        if not segment.partial and segment.text:
            async with SessionLocal() as db:
                triggered = await alarm_svc.process_speech(
                    db, camera_id, segment, hits, operator_id=int(user_id),
                    video_id=video_id,
                )
        # 逐词下发档位，前端才能做「命中词局部高亮 + 三档着色」。
        # 只给 keywords 列表的话前端无法区分 alarm / warn / highlight，
        # 用户在几千条词表下会看到满屏同色高亮，等于没有分级提示。
        await websocket.send_json({
            "type": "partial" if segment.partial else "final",
            "text": segment.text,
            "confidence": segment.confidence,
            "start": segment.start,
            "end": segment.end,
            "keywords": [h.keyword for h in hits],
            "hits": [{"w": h.keyword, "lv": h.level, "cat": h.category, "d": h.distance}
                     for h in hits],
            "level": summarize(hits)["level"],
            "alarm": triggered,
        })

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            payload = msg.get("bytes")
            if payload:
                try:
                    for segment in await session.accept(payload):
                        await emit(segment)
                except Exception as e:
                    log.warning("音频识别失败：%s", e)
                    await websocket.send_json({"type": "error", "error": "asr_runtime",
                                               "detail": str(e)})
                    break
                continue

            text = msg.get("text")
            if text:
                try:
                    ctrl = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if "camera_id" in ctrl:
                    camera_id = ctrl["camera_id"]
                # video_id 一旦设置就不再清空：它标识"本次连接属于哪个离线任务"，
                # 前端不会在会话中途切换到另一个任务。
                # 顺带把该视频关联的点位补齐，这样语音报警也能归属到正确点位，
                # 并参与"视觉+语音"的多模态互证。
                if ctrl.get("video_id"):
                    video_id = int(ctrl["video_id"])
                    if camera_id is None:
                        from app.models import VideoRecord

                        async with SessionLocal() as vdb:
                            rec = await vdb.get(VideoRecord, video_id)
                        if rec is not None:
                            camera_id = rec.camera_id
                if ctrl.get("flush"):
                    for segment in await session.finalize():
                        await emit(segment)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("音频 WS 异常：%s", e)
    finally:
        try:
            for segment in await session.finalize():
                await emit(segment)
        except Exception:
            pass
        await session.close()
