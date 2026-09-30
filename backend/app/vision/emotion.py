"""情绪识别：FER+ 模型推理 + 轨迹级时序平滑。

**模型**：FER+（ONNX Model Zoo，MIT 授权），8 类表情分类，输入 64×64 灰度图，
参数量很小 —— 单张 CPU 推理约数毫秒，适合挂在已有人脸识别链路后面。

**伦理边界（这不是注释，是设计约束）**：
    面部表情 ≠ 真实情绪，更不等于心理状态。本模块的输出**只作为值班台的实时
    辅助线索**，用于让老师更快注意到"这个孩子此刻看起来很难受"，
    绝不写入学生档案、不参与任何评价、不做心理诊断。
    因此这里刻意**不提供落库接口**：概率只在内存里存活，随轨迹消失而消失。
    前端必须显示"仅供参考，非心理诊断"的说明。

**为什么必须做时序平滑**：逐帧表情分类的抖动极大（说话、眨眼、侧头都会让类别跳变），
直接展示会得到一条乱跳的标签，值班老师会立刻失去信任。因此这里做两层平滑：
    1. 概率空间 EMA   —— 抑制单帧尖峰
    2. 滑动窗口多数投票 —— 要求类别在若干帧内稳定，才对外确认
    两者叠加后，标签变化会滞后半秒左右，但读起来是"人的表情真的变了"。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from app.core.config import settings
from app.vision.registry import resolve_weights

log = logging.getLogger("vision.emotion")

# FER+ 的 8 类固定顺序（必须与模型的输出层顺序一致，错了会整表串位）
# 索引: 0 中性 1 高兴 2 惊讶 3 悲伤 4 愤怒 5 厌恶 6 恐惧 7 轻蔑
EMOTIONS: list[tuple[str, str, str]] = [
    ("neutral", "平静", "😐"),
    ("happiness", "高兴", "😄"),
    ("surprise", "惊讶", "😲"),
    ("sadness", "悲伤", "😢"),
    ("anger", "愤怒", "😠"),
    ("disgust", "厌恶", "🤢"),
    ("fear", "恐惧", "😨"),
    ("contempt", "轻蔑", "😒"),
]

# 情绪极性：用于值班台排序（负面情绪优先展示）与颜色映射
NEGATIVE = {"sadness", "anger", "fear", "disgust", "contempt"}

EMOTION_BY_KEY = {k: (zh, icon) for k, zh, icon in EMOTIONS}


@dataclass
class EmotionResult:
    key: str
    label: str
    icon: str
    confidence: float
    negative: bool
    # 全 8 类概率，供前端画分布条（可选展示，让"依据可见"）
    probs: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "icon": self.icon,
            "confidence": round(self.confidence, 3), "negative": self.negative,
            "probs": [round(float(p), 3) for p in self.probs],
        }


class EmotionEstimator:
    """FER+ 推理器（惰性初始化，进程内单例）。"""

    def __init__(self) -> None:
        self._session = None
        self._input_name = ""
        self._path = None
        self._ready = False
        self._error = ""

    def _ensure(self) -> None:
        if self._ready:
            return
        self._ready = True

        name = (settings.EMOTION_MODEL or "").strip()
        if not name:
            self._error = "未配置 CAB_EMOTION_MODEL"
            return
        try:
            path = resolve_weights(name)
        except Exception as e:  # noqa: BLE001
            self._error = str(e)
            return
        if not path.exists():
            self._error = (f"情绪模型缺失：{path.name}。请从 ONNX Model Zoo 下载 "
                           f"emotion-ferplus-8 放入 backend/models/（含 .onnx.data 外部权重文件）")
            log.warning(self._error)
            return

        # 优先 onnxruntime：对带外部权重文件（.onnx.data）的模型支持最好；
        # 未安装时回落 cv2.dnn，功能等价、速度略慢，避免为一个可选能力强加依赖。
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            providers = ["CPUExecutionProvider"]
            try:
                if ort.get_device() == "GPU":
                    providers.insert(0, "CUDAExecutionProvider")
            except Exception:  # noqa: BLE001 —— 无 GPU 时保持 CPU
                pass
            self._session = ort.InferenceSession(str(path), sess_options=opts, providers=providers)
            self._input_name = self._session.get_inputs()[0].name
            self._backend = "onnxruntime"
        except ImportError:
            try:
                import cv2

                net = cv2.dnn.readNetFromONNX(str(path))
                self._session = net
                self._backend = "opencv-dnn"
            except Exception as e:  # noqa: BLE001
                self._error = f"情绪模型加载失败：{e}"
                log.warning(self._error)
                return
        except Exception as e:  # noqa: BLE001
            self._error = f"情绪模型加载失败：{e}"
            log.warning(self._error)
            return

        self._path = path
        log.info("情绪识别已启用：FER+（%s，后端 %s）", path.name, self._backend)

    @property
    def ready(self) -> bool:
        self._ensure()
        return self._session is not None

    def status(self) -> dict:
        self._ensure()
        return {
            "enabled": settings.ENABLE_EMOTION,
            "model": self._path.name if self._path else "",
            "classes": [zh for _, zh, _ in EMOTIONS],
            "backend": getattr(self, "_backend", ""),
            "ready": self._session is not None and settings.ENABLE_EMOTION,
            "error": self._error,
            # 明确对外声明：情绪不落库
            "persisted": False,
        }

    def infer(self, aligned_bgr: np.ndarray) -> np.ndarray | None:
        """对已对齐的人脸（SFace 输出的 112×112 BGR）推理，返回 8 类概率。

        复用 SFace 的对齐结果而不是重跑一次检测，是因为 FER+ 对五官位置同样敏感，
        而对齐后的 112×112 已经去掉了姿态与尺度差异，直接缩到 64×64 即可。
        """
        if self._session is None:
            return None
        import cv2

        try:
            gray = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY) if aligned_bgr.ndim == 3 else aligned_bgr
            img = cv2.resize(gray, (64, 64)).astype(np.float32)
            blob = img.reshape(1, 1, 64, 64)

            if getattr(self, "_backend", "") == "opencv-dnn":
                self._session.setInput(blob)
                out = self._session.forward()
            else:
                out = self._session.run(None, {self._input_name: blob})[0]
            logits = np.asarray(out, dtype=np.float32).reshape(-1)
            if logits.size != len(EMOTIONS):
                return None
            return _softmax(logits)
        except Exception as e:  # noqa: BLE001 —— 单张失败不影响主链路
            log.debug("情绪推理失败：%s", e)
            return None


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - float(np.max(x)))
    return e / max(1e-9, float(np.sum(e)))


class EmotionSmoother:
    """按轨迹维护情绪概率的 EMA + 多数投票（内存态，随轨迹回收）。"""

    def __init__(self, window: int | None = None) -> None:
        self._window = window
        self._ema: dict[int, np.ndarray] = {}
        self._votes: dict[int, deque] = {}
        self._last: dict[int, EmotionResult] = {}

    def update(self, track_id: int, probs: np.ndarray | None) -> EmotionResult | None:
        """喂入一次观测，返回平滑后的情绪（未达确认条件时返回上一次结果）。"""
        if probs is None:
            return self._last.get(track_id)

        alpha = float(settings.EMOTION_EMA_ALPHA)
        prev = self._ema.get(track_id)
        ema = probs if prev is None else alpha * probs + (1 - alpha) * prev
        self._ema[track_id] = ema

        window = self._window or int(settings.EMOTION_VOTE_WINDOW)
        votes = self._votes.get(track_id)
        if votes is None:
            votes = self._votes[track_id] = deque(maxlen=max(1, window))
        votes.append(int(np.argmax(ema)))

        idx, conf = _vote(votes, ema)
        key, label, icon = EMOTIONS[idx]
        # 置信度低于阈值时归为"平静"：与其显示一个可疑的"愤怒 0.31"，
        # 不如显示中性，避免给值班老师制造无依据的暗示。
        if conf < float(settings.EMOTION_CONF_MIN):
            key, label, icon = EMOTIONS[0]
        result = EmotionResult(key=key, label=label, icon=icon, confidence=conf,
                               negative=key in NEGATIVE, probs=ema.tolist())
        self._last[track_id] = result
        return result

    def get(self, track_id: int) -> EmotionResult | None:
        return self._last.get(track_id)

    def drop(self, track_id: int) -> None:
        self._ema.pop(track_id, None)
        self._votes.pop(track_id, None)
        self._last.pop(track_id, None)

    def gc(self, alive: set[int]) -> None:
        """回收已消失轨迹的状态，避免长跑后内存缓慢增长。"""
        for tid in [t for t in self._ema if t not in alive]:
            self.drop(tid)

    def reset(self) -> None:
        self._ema.clear()
        self._votes.clear()
        self._last.clear()


def _vote(votes: deque, ema: np.ndarray) -> tuple[int, float]:
    """窗口多数投票；票数相同时用 EMA 概率打破平局。"""
    counts: dict[int, int] = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    top = max(counts.values())
    winners = [k for k, c in counts.items() if c == top]
    if len(winners) == 1:
        idx = winners[0]
    else:
        idx = max(winners, key=lambda i: float(ema[i]))
    return idx, float(ema[idx])


emotion_estimator = EmotionEstimator()
