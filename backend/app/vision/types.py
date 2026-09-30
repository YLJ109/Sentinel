"""视觉感知层的公共数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# COCO-17 关键点索引
KP_NOSE = 0
KP_L_EYE, KP_R_EYE = 1, 2
KP_L_EAR, KP_R_EAR = 3, 4
KP_L_SHOULDER, KP_R_SHOULDER = 5, 6
KP_L_ELBOW, KP_R_ELBOW = 7, 8
KP_L_WRIST, KP_R_WRIST = 9, 10
KP_L_HIP, KP_R_HIP = 11, 12
KP_L_KNEE, KP_R_KNEE = 13, 14
KP_L_ANKLE, KP_R_ANKLE = 15, 16

SKELETON: list[tuple[int, int]] = [
    (KP_L_SHOULDER, KP_R_SHOULDER), (KP_L_SHOULDER, KP_L_ELBOW), (KP_L_ELBOW, KP_L_WRIST),
    (KP_R_SHOULDER, KP_R_ELBOW), (KP_R_ELBOW, KP_R_WRIST),
    (KP_L_SHOULDER, KP_L_HIP), (KP_R_SHOULDER, KP_R_HIP), (KP_L_HIP, KP_R_HIP),
    (KP_L_HIP, KP_L_KNEE), (KP_L_KNEE, KP_L_ANKLE),
    (KP_R_HIP, KP_R_KNEE), (KP_R_KNEE, KP_R_ANKLE),
]


@dataclass
class Detection:
    """单帧中的一个检测目标（坐标均为 0~1 归一化）。"""

    bbox: np.ndarray                 # [x1, y1, x2, y2]
    conf: float
    cls_name: str
    kpts: np.ndarray | None = None   # (17, 3) -> x, y, conf

    @property
    def center(self) -> np.ndarray:
        return np.array([(self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2])

    @property
    def height(self) -> float:
        return float(max(1e-6, self.bbox[3] - self.bbox[1]))

    @property
    def width(self) -> float:
        return float(max(1e-6, self.bbox[2] - self.bbox[0]))

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass
class Track:
    """一条轨迹：稳定的 ID + 历史序列，行为判定全部基于它。"""

    tid: int
    bbox: np.ndarray
    kpts: np.ndarray | None
    conf: float
    age: int = 0
    hits: int = 1
    misses: int = 0
    # 轨迹确认门控：连续命中足够帧数后才置 True。
    # 未确认的轨迹是"疑似目标"（多半是单帧误检），不参与渲染与行为判定。
    confirmed: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    hist: list[tuple[float, np.ndarray, np.ndarray | None]] = field(default_factory=list)
    votes: dict[str, int] = field(default_factory=dict)     # 行为 -> 连续命中帧数
    fired: dict[str, float] = field(default_factory=dict)   # 行为 -> 上次触发时间
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> np.ndarray:
        return np.array([(self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2])

    @property
    def height(self) -> float:
        return float(max(1e-6, self.bbox[3] - self.bbox[1]))

    def push(self, t: float, bbox: np.ndarray, kpts: np.ndarray | None, keep: int = 48) -> None:
        self.hist.append((t, bbox.copy(), None if kpts is None else kpts.copy()))
        if len(self.hist) > keep:
            self.hist.pop(0)

    def prev(self, steps: int = 1) -> tuple[float, np.ndarray, np.ndarray | None] | None:
        """回溯 steps 帧前的采样（用于计算速度）。"""
        if len(self.hist) <= steps:
            return None
        return self.hist[-1 - steps]

    def vote(self, name: str, hit: bool, window: int) -> int:
        """滑动投票：命中累加，未命中衰减，返回当前累积票数。"""
        cur = self.votes.get(name, 0)
        cur = min(window, cur + 1) if hit else max(0, cur - 1)
        self.votes[name] = cur
        return cur
