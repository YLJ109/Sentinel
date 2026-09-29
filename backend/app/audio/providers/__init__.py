"""ASR Provider 工厂：按配置选择识别引擎，并对上层隐藏实现差异。"""
from __future__ import annotations

import logging

from app.audio.providers.base import ASRProvider, ASRSession, SpeechSegment
from app.core.config import settings

log = logging.getLogger("audio.providers")

_instance: ASRProvider | None = None


def create_provider(name: str | None = None) -> ASRProvider:
    """按名称创建 Provider 实例。"""
    key = (name or settings.ASR_PROVIDER or "dashscope").strip().lower()
    if key in ("dashscope", "bailian", "paraformer"):
        from app.audio.providers.dashscope import DashscopeProvider

        return DashscopeProvider()
    if key in ("aliyun", "nls", "aliyun_nls"):
        from app.audio.providers.aliyun import AliyunProvider

        return AliyunProvider()
    if key in ("vosk", "local", "offline"):
        from app.audio.providers.vosk_local import VoskProvider

        return VoskProvider()
    raise ValueError(f"未知的 ASR Provider：{key}（可选 dashscope / aliyun / vosk）")


def get_provider() -> ASRProvider:
    """获取全局单例 Provider。"""
    global _instance
    if _instance is None:
        _instance = create_provider()
        log.info("ASR Provider 已选定：%s", _instance.name)
    return _instance


def reset_provider() -> None:
    """重置单例（配置热变更或测试用）。"""
    global _instance
    _instance = None


__all__ = [
    "ASRProvider", "ASRSession", "SpeechSegment",
    "create_provider", "get_provider", "reset_provider",
]
