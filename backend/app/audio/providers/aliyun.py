"""阿里云智能语音交互（NLS）实时语音识别 Provider。

接入形态：
- 鉴权：优先使用配置里直接提供的 Token；未提供时用 AccessKeyId/Secret 调用
  ``CreateToken``（RPC 签名，HMAC-SHA1）动态签发，并按过期时间缓存复用。
- 识别：与 NLS 网关建立 WebSocket 长连接，先发 ``task=start`` 控制帧，
  再持续推送 PCM16/16kHz/单声道 二进制帧；云端回传
  ``TranscriptionResultChanged``（中间结果）与 ``SentenceEnd``（成句结果）。
- 结束：发送 ``task=stop``，等待 ``TranscriptionCompleted``。

为什么用云端流式而不是整句 REST：
流式能在说话过程中持续回显中间结果，且云端自带端点检测（VAD）判定成句，
无需在服务端再实现一套静音切分逻辑；对"边说边预警"的场景延迟更低。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from app.audio.providers.base import ASRProvider, ASRSession, SpeechSegment
from app.core.config import settings

log = logging.getLogger("audio.aliyun")

TOKEN_API_VERSION = "2019-02-28"
_TOKEN_REFRESH_MARGIN = 300  # 提前 5 分钟刷新


# ---------------------------------------------------------------- 签名与令牌
def _percent_encode(value: Any) -> str:
    """阿里云 RPC 签名要求的 RFC3986 编码（空格为 %20，~ 不转义）。"""
    return urllib.parse.quote(str(value), safe="~")


def build_token_signature(secret: str, params: dict[str, Any]) -> str:
    """按阿里云 RPC 规范计算 Signature（HMAC-SHA1 + Base64）。"""
    canonical = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}"
                         for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + _percent_encode(canonical)
    digest = hmac.new((secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_token_request(access_key_id: str, access_key_secret: str,
                        endpoint: str | None = None) -> tuple[str, dict[str, str]]:
    """构造 CreateToken 请求（返回 URL 与响应头），便于单测与自检脚本复用。"""
    endpoint = (endpoint or settings.ALIYUN_TOKEN_ENDPOINT).rstrip("/")
    params: dict[str, Any] = {
        "AccessKeyId": access_key_id,
        "Action": "CreateToken",
        "Format": "JSON",
        "RegionId": settings.ALIYUN_NLS_REGION,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": TOKEN_API_VERSION,
    }
    params["Signature"] = build_token_signature(access_key_secret, params)
    query = urllib.parse.urlencode({k: params[k] for k in sorted(params)})
    return f"{endpoint}/?{query}", {"Accept": "application/json"}


class _TokenHolder:
    """NLS Token 缓存：未过期直接复用，避免每路连接都签发。"""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expire_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if settings.ALIYUN_NLS_TOKEN:
            return settings.ALIYUN_NLS_TOKEN
        if self._token and time.time() < self._expire_at - _TOKEN_REFRESH_MARGIN:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expire_at - _TOKEN_REFRESH_MARGIN:
                return self._token
            token, expire_at = await asyncio.to_thread(self._create_token)
            self._token, self._expire_at = token, expire_at
            return token

    @staticmethod
    def _create_token() -> tuple[str, float]:
        if not settings.ALIYUN_ACCESS_KEY_ID or not settings.ALIYUN_ACCESS_KEY_SECRET:
            raise RuntimeError(
                "未配置阿里云 ASR 凭据：请设置 CAB_ALIYUN_ACCESS_KEY_ID / "
                "CAB_ALIYUN_ACCESS_KEY_SECRET，或直接提供 CAB_ALIYUN_NLS_TOKEN"
            )
        url, headers = build_token_request(settings.ALIYUN_ACCESS_KEY_ID,
                                          settings.ALIYUN_ACCESS_KEY_SECRET)
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = (data.get("Token") or {}).get("Id")
        expire = (data.get("Token") or {}).get("ExpireTime")
        if not token:
            raise RuntimeError(f"CreateToken 返回异常：{data}")
        log.info("已获取阿里云 NLS Token，有效期至 %s", expire)
        return token, float(expire or (time.time() + 3600))


# ---------------------------------------------------------------- 会话
class AliyunSession(ASRSession):
    """一次实时识别会话。"""

    def __init__(self, provider: "AliyunProvider") -> None:
        self.provider = provider
        self._ws = None
        self._queue: asyncio.Queue[SpeechSegment] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._connected = False
        self._closed = False
        self._error: str | None = None
        self._started_at = time.time()

    # ---------- 连接 ----------
    async def _connect(self) -> None:
        import websockets

        token = await self.provider.token()
        url = f"{settings.ALIYUN_NLS_ENDPOINT}?token={urllib.parse.quote(token)}"
        self._ws = await websockets.connect(url, max_size=None, open_timeout=10)
        start_msg = {
            "header": {
                "appkey": settings.ALIYUN_NLS_APPKEY,
                "message_id": uuid.uuid4().hex,
                "task": "start",
                "namespace": "SpeechTranscriber",
                "format": "pcm",
                "sample_rate": settings.SPEECH_SAMPLE_RATE,
                "enable_intermediate_result": True,
                "enable_punctuation_prediction": True,
                "enable_inverse_text_normalization": True,
                "enable_voice_detection": True,
            },
            "payload": {},
        }
        await self._ws.send(json.dumps(start_msg))
        self._connected = True
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                self._handle(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._error = str(e)
            log.warning("阿里云 NLS 连接中断：%s", e)
        finally:
            self._closed = True

    def _handle(self, data: dict) -> None:
        header = data.get("header") or {}
        name = header.get("name", "")
        status = header.get("status")
        payload = data.get("payload") or {}

        # 4xxxxxxx / 5xxxxxxx 为业务或服务端错误
        if isinstance(status, int) and status >= 40000000:
            msg = header.get("status_message") or f"status={status}"
            self._error = msg
            log.error("阿里云 NLS 返回错误：%s", msg)
            return

        if name == "TranscriptionResultChanged":
            text = (payload.get("result") or "").strip()
            if text:
                self._queue.put_nowait(SpeechSegment(
                    text=text, confidence=0.0, partial=True,
                    start=self._offset(payload.get("begin_time")),
                    end=self._offset(payload.get("begin_time")) + self._dur(payload.get("time")),
                ))
        elif name == "SentenceEnd":
            text = (payload.get("result") or "").strip()
            if text:
                begin = self._offset(payload.get("begin_time"))
                self._queue.put_nowait(SpeechSegment(
                    text=text, confidence=float(payload.get("confidence") or 0.0) / 100.0,
                    partial=False, start=begin, end=begin + self._dur(payload.get("time")),
                ))
        elif name in ("TranscriptionStarted", "TranscriptionCompleted"):
            log.debug("阿里云 NLS 事件：%s", name)

    @staticmethod
    def _offset(begin_ms: Any) -> float:
        return round(float(begin_ms or 0) / 1000.0, 3)

    @staticmethod
    def _dur(ms: Any) -> float:
        return round(float(ms or 0) / 1000.0, 3)

    # ---------- 音频 ----------
    async def accept(self, pcm: bytes) -> list[SpeechSegment]:
        if not pcm:
            return []
        if not self._connected:
            await self._connect()
        try:
            await self._ws.send(pcm)
        except Exception as e:
            self._error = str(e)
            raise RuntimeError(f"向阿里云 NLS 发送音频失败：{e}") from e
        await asyncio.sleep(0)  # 让读循环有机会处理已到达的结果
        return self._drain()

    def _drain(self) -> list[SpeechSegment]:
        out: list[SpeechSegment] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    async def finalize(self) -> list[SpeechSegment]:
        if not self._connected:
            return []
        try:
            await self._ws.send(json.dumps({
                "header": {"message_id": uuid.uuid4().hex, "task": "stop"}}))
            # 等待云端把剩余结果吐完
            deadline = time.time() + 2.0
            while time.time() < deadline and not self._closed:
                await asyncio.sleep(0.05)
        except Exception as e:
            log.debug("阿里云 NLS 停止指令发送失败：%s", e)
        return self._drain()

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected = False
        self._closed = True


# ---------------------------------------------------------------- Provider
class AliyunProvider(ASRProvider):
    name = "aliyun"

    def __init__(self) -> None:
        self._tokens = _TokenHolder()
        self._error: str | None = None

    # ---------- 鉴权 ----------
    def _missing_config(self) -> str | None:
        if settings.ALIYUN_NLS_TOKEN:
            return None
        if not settings.ALIYUN_ACCESS_KEY_ID or not settings.ALIYUN_ACCESS_KEY_SECRET:
            return "缺少 AccessKey：请配置 CAB_ALIYUN_ACCESS_KEY_ID / CAB_ALIYUN_ACCESS_KEY_SECRET，或直接给出 CAB_ALIYUN_NLS_TOKEN"
        if not settings.ALIYUN_NLS_APPKEY:
            return "缺少 AppKey：请在阿里云智能语音交互控制台创建项目并配置 CAB_ALIYUN_NLS_APPKEY"
        return None

    @property
    def error(self) -> str | None:
        return self._error or self._missing_config()

    @property
    def ready(self) -> bool:
        return self.error is None

    async def prepare(self) -> None:
        """启动时完成一次鉴权，把凭据问题在启动日志里暴露出来。"""
        problem = self._missing_config()
        if problem:
            self._error = problem
            raise RuntimeError(problem)
        try:
            await self._tokens.get()
            self._error = None
        except Exception as e:
            self._error = str(e)
            raise

    def token(self):
        return self._tokens.get()

    def status(self) -> dict:
        return {
            "provider": self.name,
            "engine": "阿里云智能语音交互 (NLS 实时识别)",
            "ready": self.ready,
            "sample_rate": settings.SPEECH_SAMPLE_RATE,
            "region": settings.ALIYUN_NLS_REGION,
            "endpoint": settings.ALIYUN_NLS_ENDPOINT,
            "appkey_configured": bool(settings.ALIYUN_NLS_APPKEY),
            "credential_mode": "token" if settings.ALIYUN_NLS_TOKEN else "access_key",
            "error": self.error,
        }

    def open_session(self) -> ASRSession:
        return AliyunSession(self)
