"""人脸检测：优先使用开源 YOLO 人脸权重，缺失时回落到 YuNet，再回落 OpenCV Haar。

设计取舍：
- 这里只做**人脸定位**（检测框）。人脸识别（1:N 身份检索）在 ``vision/face_id.py``，
  两者解耦：识别关闭时检测链路完全不受影响。
- 权重可选：把开源 YOLO 人脸权重（如 ``yolov8n-face.pt``）放进 ``backend/models/``
  并把 ``CAB_FACE_MODEL`` 指过去即可自动启用；不配则用 YuNet（权重约 230KB，
  速度约为 Haar 的 5 倍，侧脸/遮挡召回更好），再不行回落 Haar，保证开箱可用、完全离线。

**关键点透出**：YuNet 每行 15 个值 = 4 个框 + **5 个关键点** + 1 个置信度。
这 5 个关键点除用于画面标注外，更重要的用途是作为 SFace ``alignCrop()`` 的对齐输入，
因此这里原样保留（``raw``，像素坐标），供 ``vision/face_id.py`` 直接使用。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from app.core.config import settings
from app.vision.registry import registry, resolve_weights

log = logging.getLogger("vision.face")

# YuNet 输出行中 5 个关键点的排布：右眼、左眼、鼻尖、右嘴角、左嘴角
YUNET_LANDMARKS = 5


def _fallback_row(bbox_px: np.ndarray) -> np.ndarray:
    """当检测后端不提供关键点（YOLO / Haar）时，用几何比例合成一行 YuNet 格式数据。

    没有关键点就无法做真正的对齐。用固定比例（双眼在 0.38 高、鼻尖在 0.5、
    嘴角在 0.72）合成出来的"近似对齐"对**正脸**仍然可用，对侧脸会明显变差 ——
    因此这只作为兜底，识别精度指标应在 YuNet 后端下测。合成结果不会更差于
    直接把整框缩放到 112×112（那是完全不对齐）。
    """
    x1, y1, x2, y2 = (float(v) for v in bbox_px[:4])
    w, h = x2 - x1, y2 - y1
    row = np.zeros(15, dtype=np.float32)
    row[0], row[1], row[2], row[3] = x1, y1, w, h
    lms = [
        (x1 + w * 0.70, y1 + h * 0.38),   # 右眼（图像坐标下的右侧）
        (x1 + w * 0.30, y1 + h * 0.38),   # 左眼
        (x1 + w * 0.50, y1 + h * 0.52),   # 鼻尖
        (x1 + w * 0.68, y1 + h * 0.74),   # 右嘴角
        (x1 + w * 0.32, y1 + h * 0.74),   # 左嘴角
    ]
    for i, (lx, ly) in enumerate(lms):
        row[4 + i * 2] = lx
        row[5 + i * 2] = ly
    row[14] = 0.5
    return row


class FaceDetector:
    """人脸检测器（惰性初始化，进程内单例）。"""

    def __init__(self) -> None:
        self._yolo = None
        self._cascade = None
        self._yunet = None
        self._yunet_path: Path | None = None
        self._yunet_size: tuple[int, int] | None = None
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
                log.warning("已配置人脸模型但权重不存在：%s，改用 YuNet", name)
            except Exception as e:
                self._error = str(e)
                log.warning("人脸模型加载失败，改用 YuNet：%s", e)

        # 未配置 YOLO 人脸权重时优先用 YuNet：OpenCV 自带 DNN 接口、零新增依赖，
        # 精度与速度都明显优于 Haar 级联
        if self._load_yunet():
            self._backend = "yunet"
            log.info("人脸检测已启用：OpenCV YuNet（%s）", self._yunet_path.name)
            return

        self._load_cascade()

    def _load_yunet(self) -> bool:
        """准备 YuNet 权重路径（真正的检测器在首次检测时按帧尺寸惰性创建）。

        YuNet 是 OpenCV 自带的 DNN 人脸检测器：接口就在 cv2 内、无需新增 pip 依赖，
        权重仅约 230KB。相比 Haar 级联，官方基准下速度快约 5 倍，
        且侧脸与遮挡场景的召回明显更好。
        """
        import cv2

        if not hasattr(cv2, "FaceDetectorYN"):
            self._error = "当前 OpenCV 版本不支持 FaceDetectorYN，回落 Haar"
            return False
        name = (settings.FACE_YUNET_MODEL or "").strip()
        if not name:
            return False
        try:
            path = resolve_weights(name)
        except Exception as e:
            self._error = str(e)
            return False
        if not path.exists():
            return False
        self._yunet_path = path
        return True

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
        return self._backend in ("yolo", "yunet", "haar")

    @property
    def has_landmarks(self) -> bool:
        """当前后端是否输出真实关键点（决定能否做真正的对齐）。"""
        self._ensure()
        return self._backend == "yunet"

    def status(self) -> dict:
        self._ensure()
        return {
            "enabled": settings.ENABLE_FACE,
            "backend": self._backend,
            "landmarks": self._backend == "yunet",
            "ready": self._backend in ("yolo", "yunet", "haar") and settings.ENABLE_FACE,
            "error": self._error,
        }

    # ---------- 检测 ----------
    def detect(self, frame: np.ndarray) -> list[dict]:
        """返回归一化人脸列表。

        每项 ``{bbox, confidence, backend, landmarks, raw}``：
        - ``bbox``       归一化 xyxy，供前端画框
        - ``landmarks``  归一化 5 点 ``[[x,y]×5]``，供前端画关键点（可能为 None）
        - ``raw``        YuNet 格式的 15 元素行（**像素坐标**），供 SFace 对齐使用
        """
        if not settings.ENABLE_FACE:
            return []
        self._ensure()
        if self._backend == "yolo":
            return self._detect_yolo(frame)
        if self._backend == "yunet":
            return self._detect_yunet(frame)
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
            px = np.array([xyxy[i][0], xyxy[i][1], xyxy[i][2], xyxy[i][3]], dtype=np.float32)
            out.append({
                "bbox": [float(px[0]) / w, float(px[1]) / h,
                         float(px[2]) / w, float(px[3]) / h],
                "confidence": round(float(confs[i]), 3),
                "backend": "yolo",
                "landmarks": None,
                "raw": _fallback_row(px),
            })
        return out

    def _detect_yunet(self, frame: np.ndarray) -> list[dict]:
        """YuNet 推理。检测器绑定输入尺寸，因此按帧尺寸惰性创建并复用。"""
        import cv2

        h, w = frame.shape[:2]
        if self._yunet is None or self._yunet_size != (w, h):
            self._yunet = cv2.FaceDetectorYN.create(
                str(self._yunet_path), "", (w, h),
                score_threshold=settings.FACE_CONF,
                nms_threshold=0.3,
                top_k=max(1, settings.FACE_MAX_FACES),
            )
            self._yunet_size = (w, h)

        _, faces = self._yunet.detect(frame)
        if faces is None:
            return []

        out: list[dict] = []
        for f in faces[: settings.FACE_MAX_FACES]:
            row = np.asarray(f, dtype=np.float32).reshape(15)
            x, y, bw, bh = (float(v) for v in row[:4])
            lms = [[float(row[4 + i * 2]) / w, float(row[5 + i * 2]) / h]
                   for i in range(YUNET_LANDMARKS)]
            out.append({
                "bbox": [max(0.0, x / w), max(0.0, y / h),
                         min(1.0, (x + bw) / w), min(1.0, (y + bh) / h)],
                # YuNet 每行 15 个值：前 4 个是框（x, y, w, h），最后一个是置信度
                "confidence": round(float(row[14]), 3),
                "backend": "yunet",
                "landmarks": lms,
                "raw": row,
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
            px = np.array([x, y, x + fw, y + fh], dtype=np.float32)
            out.append({
                "bbox": [x / w, y / h, (x + fw) / w, (y + fh) / h],
                # Haar 级联不输出置信度，用 0 表示"仅位置"，前端据此只显示标签
                "confidence": 0.0,
                "backend": "haar",
                "landmarks": None,
                "raw": _fallback_row(px),
            })
        return out


face_detector = FaceDetector()
