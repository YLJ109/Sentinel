"""语音识别 Provider 抽象。

设计目标：把"识别在哪跑"从业务代码里剥离出来。
上层（WebSocket 路由、报警编排）只依赖 ``ASRProvider`` / ``ASRSession`` 两个接口，
更换厂商只需新增一个实现并在配置里切换，不改动编排与报警逻辑。

两类接入形态都被这个接口覆盖：
- 流式厂商（阿里云 NLS 实时识别）：``accept()`` 把音频推给云端，返回增量结果
- 离线/本地厂商（Vosk）：``accept()`` 在本地解码，返回增量结果
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class SpeechSegment:
    """一段识别结果。``partial=True`` 表示流式中间结果，不参与报警判定。"""

    text: str
    confidence: float = 0.0
    start: float = 0.0
    end: float = 0.0
    speaker: str = "unknown"
    partial: bool = False
    words: list[dict] = field(default_factory=list)


class ASRSession(abc.ABC):
    """一次识别会话（每路音频一个实例）。"""

    @abc.abstractmethod
    async def accept(self, pcm: bytes) -> list[SpeechSegment]:
        """送入一块 PCM16 / 16kHz / 单声道音频，返回本次产生的文本段。"""

    @abc.abstractmethod
    async def finalize(self) -> list[SpeechSegment]:
        """结束会话，取回剩余结果。"""

    async def close(self) -> None:
        """释放连接/资源，默认空实现。"""
        return None


class ASRProvider(abc.ABC):
    """识别引擎。全局单例，模型/连接池/鉴权信息在实例内复用。"""

    name: str = "base"

    @property
    def ready(self) -> bool:
        return self.error is None

    @property
    def error(self) -> str | None:
        return None

    @abc.abstractmethod
    def status(self) -> dict:
        """运行时状态，供 /api/system/models 展示。"""

    async def prepare(self) -> None:
        """预热：加载模型或完成鉴权。失败应抛出异常。"""
        return None

    @abc.abstractmethod
    def open_session(self) -> ASRSession:
        """创建一个新的识别会话。"""

    async def transcribe(self, pcm: bytes) -> list[SpeechSegment]:
        """整段音频一次性转写（脚本自检、离线文件分析用）。"""
        session = self.open_session()
        try:
            segments = await session.accept(pcm)
            segments.extend(await session.finalize())
            return segments
        finally:
            await session.close()
