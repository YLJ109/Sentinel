"""人脸检测：优先使用开源 YOLO 人脸权重，缺失时回落到 OpenCV 自带 Haar 级联。

设计取舍：
- 只做**人脸定位**（检测框），不做人脸识别 / 特征比对。用途是画面标注、人头计数辅助，
  不采集、不存储、不比对任何生物特征，避免引入合规风险。
- 权重可选：把开源 YOLO 人脸权重（如 ``yolov8n-face.pt``）放进 ``backend/models/``
  并把 ``CAB_FACE_MODEL`` 指过去即可自动启用；不配也能用 OpenCV 自带的 Haar 级联，
  保证开箱可用、完全离线、无额外下载。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from app.core.config import settings
from app.vision.registry import registry, resolve_weights

log = logging.getLogger("vision.face")


class FaceDetector:
    """人脸检测器（惰性初始化，进程内单例）。"""

    def __init__(self) -> None:
        self._yolo = None
        self._cascade = None
        self._backend = ""
        self._error = ""
        self._ready = False

    # ---------- 初始化 ----------
    def _ensure(self) -> None:
        if self._ready:
            return
        self._ready = True  # 只尝试一次，失败也不反复刷日志

        name = (settings.FACE_MODEL or "").strip()
        if name:
            try:
                path = resolve_weights(name)
                if path.exists():
                    self._yolo = registry.get("face", name, kind="face")
                    self._backend = "yolo"
                    log.info("人脸检测已启用：YOLO 权重 %s", path.name)
                    return
                log.warning("已配置人脸模型但权重不存在：%s，回落 OpenCV 级联", name)
            except Exception as e:
                self._error = str(e)
                log.warning("人脸模型加载失败，回落 OpenCV 级联：%s", e)

        self._load_cascade()

    def _load_cascade(self) -> None:
        import cv2

        try:
            xml = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(str(xml))
            if cascade.empty():
                raise RuntimeError(f"级联文件为空：{xml}")
            self._cascade = cascade
            self._backend = "haar"
            log.info("人脸检测已启用：OpenCV Haar 级联（%s）", xml.name)
        except Exception as e:
            self._backend = "none"
            self._error = f"OpenCV 级联不可用：{e}"
            log.warning("人脸检测不可用：%s", self._error)

    # ---------- 状态 ----------
    @property
    def ready(self) -> bool:
        self._ensure()
        return self._backend in ("yolo", "haar")

    def status(self) -> dict:
        self._ensure()
        return {
            "enabled": settings.ENABLE_FACE,
            "backend": self._backend,
            "ready": self._backend in ("yolo", "haar") and settings.ENABLE_FACE,
            "error": self._error,
        }

    # ---------- 检测 ----------
    def detect(self, frame: np.ndarray) -> list[dict]:
        """返回归一化人脸框列表：``{bbox:[x1,y1,x2,y2], confidence, backend}``。"""
        if not settings.ENABLE_FACE:
            return []
        self._ensure()
        if self._backend == "yolo":
            return self._detect_yolo(frame)
        if self._backend == "haar":
            return self._detect_haar(frame)
        return []

    def _detect_yolo(self, frame: np.ndarray) -> list[dict]:
        h, w = frame.shape[:2]
        kwargs = dict(registry.predict_kwargs)
        kwargs["conf"] = settings.FACE_CONF
        res = self._yolo.predict(frame, **kwargs)[0]

        boxes = getattr(res, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.detach().cpu().numpy()
        confs = boxes.conf.detach().cpu().numpy()

        out: list[dict] = []
        for i in range(min(len(xyxy), settings.FACE_MAX_FACES)):
            out.append({
                "bbox": [float(xyxy[i][0]) / w, float(xyxy[i][1]) / h,
                         float(xyxy[i][2]) / w, float(xyxy[i][3]) / h],
                "confidence": round(float(confs[i]), 3),
                "backend": "yolo",
            })
        return out

    def _detect_haar(self, frame: np.ndarray) -> list[dict]:
        import cv2

        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 光照归一化，明显改善侧光/逆光下的召回
        gray = cv2.equalizeHist(gray)
        min_side = max(24, int(min(w, h) * settings.FACE_MIN_RATIO))

        faces = self._cascade.detectMultiScale(
            gray,
            scaleFactor=1.12,
            minNeighbors=5,
            minSize=(min_side, min_side),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        out: list[dict] = []
        for x, y, fw, fh in list(faces)[: settings.FACE_MAX_FACES]:
            out.append({
                "bbox": [x / w, y / h, (x + fw) / w, (y + fh) / h],
                # Haar 级联不输出置信度，用 0 表示"仅位置"，前端据此只显示标签
                "confidence": 0.0,
                "backend": "haar",
            })
        return out


face_detector = FaceDetector()
