"""取证文件保存：截图帧、转写文本、标注视频路径登记。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.config import settings


def _to_rgb(img: np.ndarray) -> np.ndarray:
    """统一转 RGB。感知层内部使用 BGR（OpenCV 约定），入库前在这里转换。"""
    if img.ndim == 2:
        return np.stack([img] * 3, axis=-1)
    if img.shape[2] == 4:
        return img[:, :, :3][:, :, ::-1]
    return img[:, :, ::-1]


def save_frame(image: np.ndarray, prefix: str = "frame") -> str:
    """保存一帧取证截图（入参为 BGR），返回相对 data 目录路径。"""
    ts = int(time.time() * 1000)
    name = f"{prefix}_{ts}.jpg"
    out = settings.EVIDENCE_DIR / name
    Image.fromarray(_to_rgb(image)).save(out, quality=settings.EVIDENCE_JPEG_QUALITY)
    return f"evidence/{name}"


def save_transcript(text: str, prefix: str = "transcript") -> str:
    """保存转写文本，返回相对 data 目录路径。"""
    ts = int(time.time() * 1000)
    name = f"{prefix}_{ts}.txt"
    out = settings.EVIDENCE_DIR / name
    out.write_text(text, encoding="utf-8")
    return f"evidence/{name}"


def register_evidence_paths(paths: list[str]) -> str:
    """将取证路径列表序列化为可入库的字符串。"""
    return json.dumps(paths, ensure_ascii=False)


def parse_evidence_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def clip_rel_path(name: str) -> str:
    return f"clips/{Path(name).name}"
