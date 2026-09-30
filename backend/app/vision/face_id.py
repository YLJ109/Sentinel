"""人脸识别：OpenCV SFace 特征提取 + 注册照质量门。

为什么选 SFace 而不是 InsightFace/ArcFace：

1. **授权干净**。SFace 与其配套检测器 YuNet 同为 OpenCV Zoo 出品，Apache 2.0，
   可商用；InsightFace 的 buffalo 系列代码是 MIT 但**预训练权重仅限非商业学术研究**。
2. **零新增依赖**。``cv2.FaceRecognizerSF`` 就在 OpenCV 里，本项目已依赖 OpenCV。
3. **与现有检测器天然衔接**。YuNet 每行输出 15 个值 = 4 框 + **5 关键点** + 置信度，
   而这 5 个关键点正好是 ``alignCrop()`` 需要的对齐输入。也就是说不需要引入第二个
   检测模型、不需要手写相似变换矩阵 —— 而官方实践反复强调"预处理错配是复现不了
   识别指标的首要原因"，复用同一检测器的关键点恰好回避了这个坑。

特征向量为 128 维，已 L2 归一化，检索时余弦相似度 = 点积。
官方给出的余弦判定阈值是 0.363；本项目默认取更保守的 0.42（见 settings）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from app.core.config import settings
from app.vision.registry import resolve_weights

log = logging.getLogger("vision.face_id")


@dataclass
class QualityReport:
    """注册照质量报告。``ok=False`` 时 reasons 里给出人话原因。"""

    ok: bool = False
    score: float = 0.0                 # 0~1 综合质量分
    face_px: int = 0                   # 人脸短边像素
    sharpness: float = 0.0             # Laplacian 方差
    brightness: float = 0.0            # 灰度均值
    pose_offset: float = 0.0           # 正脸偏移比（越小越正）
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "score": round(self.score, 3), "face_px": self.face_px,
            "sharpness": round(self.sharpness, 1), "brightness": round(self.brightness, 1),
            "pose_offset": round(self.pose_offset, 3), "reasons": self.reasons,
        }


class FaceIdentity:
    """SFace 封装（惰性初始化，进程内单例）。"""

    def __init__(self) -> None:
        self._model = None
        self._path = None
        self._ready = False
        self._error = ""

    # ---------- 初始化 ----------
    def _ensure(self) -> None:
        if self._ready:
            return
        self._ready = True

        import cv2

        if not hasattr(cv2, "FaceRecognizerSF"):
            self._error = "当前 OpenCV 版本不支持 FaceRecognizerSF，人脸识别不可用"
            log.warning(self._error)
            return

        name = (settings.FACE_SFACE_MODEL or "").strip()
        if not name:
            self._error = "未配置 CAB_FACE_SFACE_MODEL"
            return
        try:
            path = resolve_weights(name)
        except Exception as e:  # noqa: BLE001
            self._error = str(e)
            log.warning("人脸特征模型路径解析失败：%s", e)
            return
        if not path.exists():
            # 权重不是自动下载的（约 37MB，需从 OpenCV Zoo 获取），因此只提示不强求：
            # 识别能力缺失不影响检测、行为判定与报警链路。
            self._error = (f"特征模型缺失：{path.name}。请从 OpenCV Zoo 下载 "
                           f"face_recognition_sface_2021dec.onnx 放入 backend/models/")
            log.warning(self._error)
            return

        try:
            self._model = cv2.FaceRecognizerSF.create(str(path), "")
            self._path = path
            log.info("人脸识别已启用：OpenCV SFace（%s）", path.name)
        except Exception as e:  # noqa: BLE001
            self._error = f"SFace 加载失败：{e}"
            log.warning(self._error)

    @property
    def ready(self) -> bool:
        self._ensure()
        return self._model is not None

    def status(self) -> dict:
        self._ensure()
        return {
            "enabled": settings.ENABLE_FACE_ID,
            "backend": "sface",
            "model": self._path.name if self._path else "",
            "dim": 128,
            "ready": self._model is not None and settings.ENABLE_FACE_ID,
            "error": self._error,
        }

    # ---------- 对齐与特征 ----------
    def align(self, frame: np.ndarray, face_row: np.ndarray) -> np.ndarray | None:
        """按 5 关键点做人脸对齐并裁到 112×112（SFace 的固定输入规格）。"""
        if self._model is None:
            return None
        import cv2

        try:
            row = np.asarray(face_row, dtype=np.float32).reshape(1, 15)
            aligned = self._model.alignCrop(frame, row)
            if aligned is None or aligned.size == 0:
                return None
            if aligned.shape[0] != 112 or aligned.shape[1] != 112:
                aligned = cv2.resize(aligned, (112, 112))
            return aligned
        except Exception as e:  # noqa: BLE001 —— 单张人脸失败不应影响整帧
            log.debug("人脸对齐失败：%s", e)
            return None

    def feature(self, frame: np.ndarray, face_row: np.ndarray) -> np.ndarray | None:
        """提取 128 维 L2 归一化特征。"""
        aligned = self.align(frame, face_row)
        if aligned is None:
            return None
        return self.feature_from_aligned(aligned)

    def feature_from_aligned(self, aligned: np.ndarray) -> np.ndarray | None:
        if self._model is None:
            return None
        try:
            vec = np.asarray(self._model.feature(aligned), dtype=np.float32).reshape(-1)
        except Exception as e:  # noqa: BLE001
            log.debug("人脸特征提取失败：%s", e)
            return None
        norm = float(np.linalg.norm(vec))
        if norm < 1e-6:
            return None
        return vec / norm

    # ---------- 质量门 ----------
    def quality(self, frame: np.ndarray, face: dict) -> QualityReport:
        """注册照质量校验：不合格的图直接拒绝入库，而不是"存下去以后再说"。

        单张图片注册的成败几乎完全取决于这一关。四项检查都对应一个具体的失败模式：
          - 人脸太小     → 特征里几乎没有可区分的信息，检索必然撞车
          - 模糊/运动拖影 → 特征退化，同一个人也不相似
          - 过暗/过曝     → 对比度丢失，等价于模糊
          - 侧脸/俯仰     → 关键点位置偏离，对齐后五官错位
        """
        report = QualityReport()
        raw = face.get("raw")
        if raw is None:
            report.reasons.append("未拿到人脸关键点，无法评估质量")
            return report
        row = np.asarray(raw, dtype=np.float32).reshape(15)

        # 1) 人脸尺寸（用原始像素框，归一化坐标会随分辨率变化）
        w_px, h_px = float(row[2]), float(row[3])
        report.face_px = int(min(w_px, h_px))
        min_px = int(settings.FACE_MIN_FACE_PX)
        if report.face_px < min_px:
            report.reasons.append(f"人脸过小（{report.face_px}px，需 ≥{min_px}px），请靠近重拍")

        # 2) 清晰度与亮度：在**对齐后的 112×112 灰度图**上算，避免背景干扰
        aligned = self.align(frame, row)
        if aligned is None:
            report.reasons.append("人脸对齐失败，图片可能损坏或人脸区域越界")
            return report
        gray = _to_gray(aligned)
        report.sharpness = float(_laplacian_var(gray))
        report.brightness = float(gray.mean())
        if report.sharpness < settings.FACE_MIN_SHARPNESS:
            report.reasons.append(f"图片不清晰（清晰度 {report.sharpness:.0f}，需 ≥{settings.FACE_MIN_SHARPNESS:.0f}）")
        if report.brightness < 40:
            report.reasons.append("画面过暗，请在光线充足处重拍")
        elif report.brightness > 225:
            report.reasons.append("画面过曝，请避开强光直射")

        # 3) 正脸：双眼中点相对人脸中心的水平偏移比。
        #    侧脸时一只眼被压缩，双眼中点会明显偏向一侧，比值随之变大。
        eye_x = (float(row[4]) + float(row[6])) / 2
        center_x = float(row[0]) + w_px / 2
        report.pose_offset = abs(eye_x - center_x) / max(1.0, w_px)
        if report.pose_offset > settings.FACE_MAX_POSE_RATIO:
            report.reasons.append("人脸偏侧，请正对摄像头重拍")

        # 4) 五官几何合理性：两眼间距应占人脸宽度的 25%~60%。
        #    这个区间之外的检测框多半是把"两只眼+半张脸"或"整颗头"当成了脸。
        eye_dist = abs(float(row[6]) - float(row[4]))
        ratio = eye_dist / max(1.0, w_px)
        if not (0.20 <= ratio <= 0.65):
            report.reasons.append("人脸区域异常，请换一张正面清晰照片")

        # 综合分：四项子分加权，用于前端进度条与"补拍质量对比"
        size_s = min(1.0, report.face_px / max(1.0, min_px * 1.6))
        sharp_s = min(1.0, report.sharpness / max(1.0, settings.FACE_MIN_SHARPNESS * 2.5))
        pose_s = max(0.0, 1.0 - report.pose_offset / max(1e-6, settings.FACE_MAX_POSE_RATIO))
        bright_s = max(0.0, 1.0 - abs(report.brightness - 128.0) / 128.0)
        report.score = round(0.35 * size_s + 0.30 * sharp_s + 0.20 * pose_s + 0.15 * bright_s, 3)
        report.ok = not report.reasons
        return report


def _to_gray(img: np.ndarray) -> np.ndarray:
    import cv2

    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _laplacian_var(gray: np.ndarray) -> float:
    """Laplacian 方差：经典的清晰度指标，越大越清晰。"""
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """两个已归一化特征的余弦相似度（等价于点积，加了保险的归一化）。"""
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


face_id = FaceIdentity()
