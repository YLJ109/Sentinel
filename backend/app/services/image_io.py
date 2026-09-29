"""图像 base64 编解码工具。

约定：视觉链路统一使用 **BGR** 通道顺序（与 OpenCV 及 ultralytics 的 numpy 输入一致）。
浏览器送来的 JPEG 经 PIL 解码为 RGB，因此在这里翻转通道，避免下游出现颜色错乱。
"""
from __future__ import annotations

import base64
import binascii
from io import BytesIO

import numpy as np
from PIL import Image


def b64_to_image(b64: str) -> np.ndarray | None:
    """base64（可含 data URL 前缀）→ BGR ndarray。"""
    try:
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        raw = base64.b64decode(b64)
        rgb = np.array(Image.open(BytesIO(raw)).convert("RGB"))
        return rgb[:, :, ::-1].copy()
    except (binascii.Error, ValueError, OSError):
        return None


def image_to_b64(img: np.ndarray, quality: int = 80) -> str:
    """BGR ndarray → base64 JPEG。"""
    buf = BytesIO()
    Image.fromarray(img[:, :, ::-1]).save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")
