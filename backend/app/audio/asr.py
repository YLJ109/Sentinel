"""语音识别门面。

上层只与本模块交互，不感知具体厂商：
- 通过配置 ``CAB_ASR_PROVIDER`` 在 ``aliyun``（云端流式）与 ``vosk``（本地兜底）之间切换
- ``asr.status()`` 供 ``/api/system/models`` 展示引擎就绪情况
- ``asr.open_session()`` 为每路音频创建独立会话
"""
from __future__ import annotations

import asyncio
import logging

from app.audio.providers import ASRSession, SpeechSegment, get_provider
from app.core.config import settings

log = logging.getLogger("audio.asr")


class _ASRFacade:
    """对 Provider 的薄封装，保持调用方接口稳定。"""

    @property
    def provider(self):
        return get_provider()

    @property
    def ready(self) -> bool:
        return self.provider.ready

    @property
    def error(self) -> str | None:
        return self.provider.error

    def status(self) -> dict:
        return self.provider.status()

    async def prepare(self) -> None:
        await self.provider.prepare()

    def open_session(self) -> ASRSession:
        return self.provider.open_session()

    async def transcribe(self, pcm: bytes) -> list[SpeechSegment]:
        return await self.provider.transcribe(pcm)

    def prepare_in_background(self) -> None:
        """启动时不阻塞：后台完成鉴权/加载，失败只记录日志。"""
        if not settings.SPEECH_ENABLED:
            log.info("语音识别已按配置关闭（CAB_SPEECH_ENABLED=false）")
            return

        async def _run() -> None:
            try:
                await self.prepare()
                log.info("语音识别就绪：%s", self.provider.name)
            except Exception as e:
                log.warning("语音识别未就绪（语音链路将不可用）：%s", e)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_run())


asr = _ASRFacade()

__all__ = ["asr", "SpeechSegment", "ASRSession"]
