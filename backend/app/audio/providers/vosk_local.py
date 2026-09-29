"""Vosk 离线识别 Provider（本地兜底）。

用途：无外网 / 未配置云端凭据时的降级方案，也用于完全离线的演示环境。
把音频留在本地是它的优势，但中文小模型准确率明显低于云端，因此默认不作为主链路。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from app.audio.providers.base import ASRProvider, ASRSession, SpeechSegment
from app.core.config import settings

log = logging.getLogger("audio.vosk")


def _model_present(path: Path) -> bool:
    return path.is_dir() and (path / "conf").is_dir() and (path / "am").is_dir()


def _download(url: str, dest: Path) -> None:
    log.info("开始下载语音模型：%s", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = {"pct": -10}

    def hook(blocks: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(blocks * block_size * 100 / total)
        if pct - last["pct"] >= 10:
            last["pct"] = pct
            log.info("语音模型下载进度 %d%%", min(pct, 100))

    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    tmp.replace(dest)


def ensure_model() -> Path:
    """确保 Vosk 模型就绪，返回模型目录。"""
    target = settings.vosk_model_path
    if _model_present(target):
        return target

    zip_path = settings.MODELS_DIR / f"{settings.VOSK_MODEL_NAME}.zip"
    if not zip_path.exists():
        _download(settings.VOSK_MODEL_URL, zip_path)

    log.info("解压语音模型 %s", zip_path.name)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(settings.MODELS_DIR)

    if not _model_present(target):
        for child in settings.MODELS_DIR.iterdir():
            if child.is_dir() and _model_present(child):
                if child != target:
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    child.rename(target)
                break

    if not _model_present(target):
        raise RuntimeError(f"语音模型解压后未找到有效目录：{target}")
    return target


class VoskSession(ASRSession):
    """本地流式识别会话（Kaldi 解码在 CPU 上进行）。"""

    def __init__(self, model, sample_rate: int) -> None:
        from vosk import KaldiRecognizer

        self.rec = KaldiRecognizer(model, sample_rate)
        self.rec.SetWords(True)
        try:
            self.rec.SetPartialWords(True)
        except Exception:
            pass
        self.sample_rate = sample_rate

    async def accept(self, pcm: bytes) -> list[SpeechSegment]:
        if not pcm:
            return []
        # Kaldi 解码是 CPU 密集的同步调用，放到线程池避免阻塞事件循环
        return await asyncio.to_thread(self._accept_sync, pcm)

    def _accept_sync(self, pcm: bytes) -> list[SpeechSegment]:
        if self.rec.AcceptWaveform(pcm):
            return self._segments(json.loads(self.rec.Result()), partial=False)
        partial = json.loads(self.rec.PartialResult()).get("partial", "").strip()
        return [SpeechSegment(text=partial, confidence=0.0, partial=True)] if partial else []

    async def finalize(self) -> list[SpeechSegment]:
        return await asyncio.to_thread(lambda: self._segments(json.loads(self.rec.FinalResult()), False))

    @staticmethod
    def _segments(data: dict, partial: bool) -> list[SpeechSegment]:
        text = (data.get("text") or "").strip()
        if not text:
            return []
        words = data.get("result") or []
        confs = [float(w.get("conf", 0.0)) for w in words if w.get("conf") is not None]
        conf = sum(confs) / len(confs) if confs else 0.0
        start = float(words[0].get("start", 0.0)) if words else 0.0
        end = float(words[-1].get("end", start)) if words else start
        return [SpeechSegment(text=text, confidence=round(conf, 3), start=start, end=end,
                              partial=partial, words=words)]


class VoskProvider(ASRProvider):
    name = "vosk"

    def __init__(self) -> None:
        self._model = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)
            path = ensure_model()
            self._model = Model(str(path))
            log.info("Vosk 本地模型加载完成：%s", path.name)
        return self._model

    async def prepare(self) -> None:
        try:
            await asyncio.to_thread(self._load)
            self._error = None
        except Exception as e:
            self._error = str(e)
            raise

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def ready(self) -> bool:
        return self._model is not None

    def status(self) -> dict:
        return {
            "provider": self.name,
            "engine": "Vosk 离线识别（本地兜底）",
            "ready": self.ready,
            "model": settings.VOSK_MODEL_NAME,
            "path": str(settings.vosk_model_path),
            "sample_rate": settings.SPEECH_SAMPLE_RATE,
            "error": self._error,
        }

    def open_session(self) -> ASRSession:
        return VoskSession(self._load(), settings.SPEECH_SAMPLE_RATE)
