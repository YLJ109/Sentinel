"""YOLO 权重解析、推理设备选择与模型生命周期管理。

职责：
- 把配置里的权重名解析到 ``backend/models/`` 目录（不存在时由 ultralytics 自动下载）
- 自动选择推理设备（cuda:0 / cpu），并在 GPU 上启用 FP16
- 惰性加载 + 缓存模型实例，避免重复占用显存
- 预热（首次推理包含 CUDA 上下文初始化与 kernel 编译，必须提前做掉）
- 对外暴露可观测的模型状态，供 ``/api/system/models`` 使用
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings

log = logging.getLogger("vision.registry")


def resolve_device(prefer: str | None = None) -> str:
    """确定推理设备；``auto`` 时优先 GPU。"""
    want = str(prefer if prefer is not None else (settings.DEVICE or "auto")).strip()
    if want and want != "auto":
        return want
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:  # torch 不可用时退回 CPU
        return "cpu"


def device_info() -> dict[str, Any]:
    """返回推理后端信息，用于健康检查与前端展示。"""
    info: dict[str, Any] = {"device": resolve_device()}
    try:
        import torch

        info.update(
            {
                "torch": torch.__version__,
                "cuda_built": torch.version.cuda,
                "cuda_available": bool(torch.cuda.is_available()),
            }
        )
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            info["gpu"] = torch.cuda.get_device_name(0)
            info["gpu_memory_gb"] = round(props.total_memory / 1024**3, 1)
    except Exception as e:
        info["error"] = str(e)
    return info


def resolve_weights(name: str) -> Path:
    """把权重标识解析为 ``models/`` 下的绝对路径。

    - 传绝对路径：原样返回
    - 传文件名（如 ``yolov8n.pt``）：定位到 ``models/yolov8n.pt``，
      不存在时 ultralytics 会按官方资源名下载到该位置
    """
    if not name:
        raise ValueError("权重名不能为空")
    p = Path(name)
    if p.is_absolute():
        return p
    return settings.MODELS_DIR / p.name


@dataclass
class ModelState:
    key: str
    weights: str
    path: str
    kind: str
    loaded: bool = False
    error: str | None = None


class ModelRegistry:
    """YOLO 模型注册表：惰性加载、缓存、统一设备配置。"""

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._states: dict[str, ModelState] = {}
        self._device: str | None = None
        self._warmed = False

    # ---------- 设备 ----------
    @property
    def device(self) -> str:
        if self._device is None:
            self._device = resolve_device()
        return self._device

    @property
    def half(self) -> bool:
        return bool(settings.HALF and self.device.startswith("cuda"))

    @property
    def predict_kwargs(self) -> dict[str, Any]:
        """统一的推理超参。"""
        return {
            "conf": settings.DETECT_CONF,
            "iou": settings.IOU_THRESHOLD,
            "imgsz": settings.IMGSZ,
            "max_det": settings.MAX_DET,
            "device": self.device,
            "half": self.half,
            "verbose": False,
        }

    # ---------- 加载 ----------
    def get(self, key: str, weights: str, kind: str = "detect"):
        """按 key 获取（必要时加载）模型实例。"""
        if key in self._models:
            return self._models[key]

        path = resolve_weights(weights)
        state = ModelState(key=key, weights=weights, path=str(path), kind=kind)
        self._states[key] = state

        from ultralytics import YOLO

        try:
            model = YOLO(str(path))
        except Exception as e:
            state.error = f"权重加载失败：{e}"
            log.error("模型加载失败 key=%s weights=%s err=%s", key, weights, e)
            raise

        try:
            # 显式绑定设备，避免 ultralytics 每次推理重新推断
            model.to(self.device)
        except Exception as e:  # 设备不可用时保持默认（CPU）
            log.warning("模型绑定设备 %s 失败，回退默认：%s", self.device, e)

        self._models[key] = model
        state.loaded = True
        log.info("模型已加载 key=%s weights=%s device=%s half=%s", key, path.name, self.device, self.half)
        return model

    def detect_model(self):
        return self.get("detect", settings.YOLO_DETECT_WEIGHTS, kind="detect")

    def pose_model(self):
        return self.get("pose", settings.YOLO_POSE_WEIGHTS, kind="pose")

    def behavior_model(self):
        """自定义行为模型（烟/暴力等），未配置或缺失时返回 None。"""
        name = (settings.YOLO_BEHAVIOR_WEIGHTS or "").strip()
        if not name:
            return None
        if not resolve_weights(name).exists():
            log.warning("已配置自定义行为模型但权重不存在：%s", name)
            self._states["behavior"] = ModelState(
                key="behavior", weights=name, path=str(resolve_weights(name)),
                kind="behavior", loaded=False, error="权重文件不存在",
            )
            return None
        return self.get("behavior", name, kind="behavior")

    # ---------- 设备切换 ----------
    def use_device(self, device: str) -> None:
        """仅设置目标设备，不立即加载（由随后的 warmup 完成加载）。"""
        self._device = resolve_device(device)
        self._warmed = False

    def switch_device(self, device: str) -> dict[str, Any]:
        """热切换推理设备：丢弃已加载模型 → 按新设备重新加载 → 重新预热。

        模型对象与设备是绑定的（``model.to(device)``），所以不能只改设备标识，
        必须整批重载；旧模型由 GC 回收，CUDA 显存随之释放。
        """
        previous = self.device
        self._models.clear()
        self._states.clear()
        self._device = resolve_device(device)
        self._warmed = False
        self.warmup()
        log.info("推理设备已切换：%s -> %s", previous, self.device)
        return {"previous": previous, "device": self.device, "half": self.half}

    # ---------- 预热 ----------
    def warmup(self) -> None:
        """用一张空白图跑一次推理，完成 CUDA 上下文与 kernel 预热。"""
        if self._warmed:
            return
        import numpy as np

        dummy = np.zeros((settings.IMGSZ, settings.IMGSZ, 3), dtype=np.uint8)
        for loader in (self.detect_model, self.pose_model):
            try:
                model = loader()
                model.predict(dummy, **self.predict_kwargs)
            except Exception as e:
                log.warning("模型预热失败（不影响启动）：%s", e)
        self._warmed = True
        log.info("模型预热完成 device=%s", self.device)

    # ---------- 状态 ----------
    def status(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "half": self.half,
            "imgsz": settings.IMGSZ,
            "device_info": device_info(),
            "models": {
                k: {
                    "weights": s.weights,
                    "path": s.path,
                    "kind": s.kind,
                    "loaded": s.loaded,
                    "error": s.error,
                }
                for k, s in self._states.items()
            },
        }


registry = ModelRegistry()
