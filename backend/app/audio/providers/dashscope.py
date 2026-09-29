"""阿里云百炼（DashScope）Paraformer 实时语音识别 Provider。

与「智能语音交互（NLS）」是两套完全不同的接入，别混用：

====================  ==============================  ==============================
                      NLS（智能语音交互）            百炼 DashScope（本文件）
====================  ==============================  ==============================
地址                  nls-gateway-*.aliyuncs.com      dashscope.aliyuncs.com/api-ws/v1
凭据                  AppKey + Token（AccessKey 签）  API Key（``sk-`` 开头）
鉴权方式              URL 查询参数 ``?token=``        请求头 ``Authorization: bearer``
音频上行              PCM 二进制帧                    PCM 二进制帧（见下方注意）
====================  ==============================  ==============================

**实测注意**：音频必须以**二进制帧**直接发送。把 PCM 放进 ``continue-task`` 的
``payload.input.audio``（base64）会被云端判为 ``NO_VALID_AUDIO_ERROR``，这一点与
部分文档示例不一致，改动上行方式前请先跑 ``scripts/check_asr.py`` 回归。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from app.audio.providers.base import ASRProvider, ASRSession, SpeechSegment
from app.core.config import settings

log = logging.getLogger("audio.dashscope")


class DashscopeSession(ASRSession):
    """一次实时识别会话：run-task → 二进制推流 → finish-task。"""

    def __init__(self, provider: "DashscopeProvider") -> None:
        self.provider = provider
        self._ws: Any = None
        self._queue: asyncio.Queue[SpeechSegment] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._connected = False
        self._closed = False
        self._error: str | None = None
        self._task_id = uuid.uuid4().hex

    # ---------- 控制帧 ----------
    def _frame(self, action: str, payload: dict) -> str:
        return json.dumps({
            "header": {"action": action, "task_id": self._task_id, "streaming": "duplex"},
            "payload": payload,
        })

    @staticmethod
    def _event_of(raw: Any) -> tuple[str, str]:
        """从一帧里取出 (event 名, 错误信息)，二进制帧返回空。"""
        if isinstance(raw, (bytes, bytearray)):
            return "", ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return "", ""
        header = data.get("header") or {}
        message = header.get("error_message") or header.get("error_code") or ""
        return str(header.get("event") or ""), str(message)

    # ---------- 连接 ----------
    async def ensure_connected(self) -> None:
        if self._connected:
            return
        import websockets

        self._ws = await websockets.connect(
            settings.DASHSCOPE_WS_URL,
            extra_headers={"Authorization": f"bearer {settings.DASHSCOPE_API_KEY}"},
            max_size=None,
            open_timeout=10,
        )
        await self._ws.send(self._frame("run-task", {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": settings.DASHSCOPE_ASR_MODEL,
            "parameters": {
                "format": "pcm",
                "sample_rate": settings.SPEECH_SAMPLE_RATE,
                "language_hints": ["zh"],
            },
            "input": {},
        }))

        # 必须等到 task-started 才能推流；task-failed 时把云端原始原因抛给上层
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            event, message = self._event_of(raw)
            if event == "task-started":
                break
            if event == "task-failed":
                raise RuntimeError(message or "云端拒绝了本次识别任务")

        self._connected = True
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                self._handle(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._error = str(e)
            log.warning("百炼 ASR 连接中断：%s", e)
        finally:
            self._closed = True

    def _handle(self, data: dict) -> None:
        header = data.get("header") or {}
        event = header.get("event")

        if event == "result-generated":
            sentence = ((data.get("payload") or {}).get("output") or {}).get("sentence") or {}
            text = str(sentence.get("text") or "").strip()
            if not text:
                return
            # sentence_end=True 为成句结果（参与报警判定），否则是流式中间结果
            final = bool(sentence.get("sentence_end"))
            begin = round(float(sentence.get("begin_time") or 0) / 1000.0, 3)
            finish = round(float(sentence.get("end_time") or 0) / 1000.0, 3)
            self._queue.put_nowait(SpeechSegment(
                text=text, confidence=0.0, start=begin,
                end=finish if finish > begin else begin, partial=not final,
            ))
        elif event == "task-failed":
            self._error = str(header.get("error_message") or header.get("error_code") or "识别失败")
            log.error("百炼 ASR 返回错误：%s", self._error)
        elif event == "task-finished":
            log.debug("百炼 ASR 任务结束")

    # ---------- 音频 ----------
    async def accept(self, pcm: bytes) -> list[SpeechSegment]:
        if not pcm:
            return []
        if not self._connected:
            await self.ensure_connected()
        try:
            # 二进制帧直发（见模块顶部说明，base64 方式会被判为无效音频）
            await self._ws.send(pcm)
        except Exception as e:
            self._error = str(e)
            raise RuntimeError(f"向百炼发送音频失败：{e}") from e
        await asyncio.sleep(0)
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
            await self._ws.send(self._frame("finish-task", {"input": {}}))
            # 等云端把剩余结果吐完（通常几十毫秒）
            deadline = time.time() + 3.0
            while time.time() < deadline and not self._closed:
                await asyncio.sleep(0.05)
        except Exception as e:
            log.debug("百炼停止指令发送失败：%s", e)
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


class DashscopeProvider(ASRProvider):
    name = "dashscope"

    def __init__(self) -> None:
        self._error: str | None = None

    # ---------- 凭据 ----------
    def _missing_config(self) -> str | None:
        if not settings.DASHSCOPE_API_KEY:
            return ("缺少百炼 API Key：请在百炼控制台创建 API-KEY（sk- 开头）"
                    "并配置 CAB_DASHSCOPE_API_KEY")
        return None

    @property
    def error(self) -> str | None:
        return self._error or self._missing_config()

    @property
    def ready(self) -> bool:
        return self.error is None

    async def prepare(self) -> None:
        """建连 + run-task 验证凭据与模型，把鉴权问题在启动日志里暴露出来。"""
        problem = self._missing_config()
        if problem:
            self._error = problem
            raise RuntimeError(problem)
        session = DashscopeSession(self)
        try:
            await session.ensure_connected()
            self._error = None
        finally:
            await session.close()

    def status(self) -> dict:
        return {
            "provider": self.name,
            "engine": "阿里云百炼 Paraformer 实时识别",
            "ready": self.ready,
            "sample_rate": settings.SPEECH_SAMPLE_RATE,
            "model": settings.DASHSCOPE_ASR_MODEL,
            "endpoint": settings.DASHSCOPE_WS_URL,
            "credential_mode": "api_key",
            "error": self.error,
        }

    def open_session(self) -> ASRSession:
        return DashscopeSession(self)
