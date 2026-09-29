"""多目标跟踪：IoU + 匈牙利匹配，带匀速预测与轨迹生命周期管理。

为什么不用 ultralytics 内置的 ByteTrack：
``model.track(persist=True)`` 的跟踪器状态挂在模型实例上，多路摄像头交替送帧会互相污染。
这里为每个摄像头维护独立跟踪器实例，关联代价在 IoU 基础上叠加中心距惩罚，
并用指数滑动平均估计速度做一步预测，改善快速移动人员的关联稳定性。
"""
from __future__ import annotations

import logging

import numpy as np

from app.core.config import settings
from app.vision.types import Detection, Track

log = logging.getLogger("vision.tracker")


def iou(a: np.ndarray, b: np.ndarray) -> float:
    """两个 xyxy 框的交并比。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0


class IoUTracker:
    """单路视频的多目标跟踪器。"""

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_miss: int | None = None,
        ema_alpha: float = 0.65,
        dist_gate: float = 0.28,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_miss = max_miss or settings.TRACK_TTL_FRAMES
        self.ema_alpha = ema_alpha      # 速度平滑系数
        self.dist_gate = dist_gate      # 中心距门限（归一化），超过视为不同目标
        self.tracks: dict[int, Track] = {}
        self._next_id = 1
        self._vel: dict[int, np.ndarray] = {}

    # ---------- 主流程 ----------
    def update(self, detections: list[Detection], t: float) -> list[Track]:
        """用当前帧检测结果更新轨迹，返回本帧活跃轨迹。"""
        existing_ids = set(self.tracks.keys())
        preds = {tid: self._predict(tr) for tid, tr in self.tracks.items()}
        matches, unmatched_dets = self._associate(preds, detections)

        for tid, di in matches:
            self._update_track(self.tracks[tid], detections[di], t)

        for di in unmatched_dets:
            self._spawn(detections[di], t)

        # 未匹配轨迹失活。注意只对"本帧之前就存在"的轨迹计未命中，
        # 否则刚新建的轨迹会在诞生当帧就被记为 misses=1，导致首帧目标被过滤掉。
        matched_ids = {tid for tid, _ in matches}
        for tid in existing_ids:
            if tid not in matched_ids:
                self.tracks[tid].misses += 1
                if self.tracks[tid].misses > self.max_miss:
                    self.tracks.pop(tid, None)
                    self._vel.pop(tid, None)

        for tr in self.tracks.values():
            tr.age += 1
        return list(self.tracks.values())

    def reset(self) -> None:
        self.tracks.clear()
        self._vel.clear()
        self._next_id = 1

    # ---------- 关联 ----------
    def _predict(self, tr: Track) -> np.ndarray:
        """用平滑速度外推一步，缓解快速移动导致的 IoU 骤降。"""
        vel = self._vel.get(tr.tid)
        if vel is None:
            return tr.bbox.copy()
        return tr.bbox.copy() + np.array([vel[0], vel[1], vel[0], vel[1]])

    def _associate(
        self, preds: dict[int, np.ndarray], detections: list[Detection]
    ) -> tuple[list[tuple[int, int]], list[int]]:
        if not self.tracks or not detections:
            return [], list(range(len(detections)))

        tids = list(preds.keys())
        cost = np.zeros((len(tids), len(detections)), dtype=np.float64)
        for r, tid in enumerate(tids):
            pb = preds[tid]
            pc = np.array([(pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2])
            for c, det in enumerate(detections):
                overlap = iou(pb, det.bbox)
                dist = float(np.linalg.norm(pc - det.center))
                # 代价 = 1 - IoU，中心距越远代价越高；超出距离门限直接判为不可匹配
                penalty = 0.0 if dist <= self.dist_gate else 1.0
                cost[r, c] = (1.0 - overlap) + penalty + dist * 0.35

        try:
            from scipy.optimize import linear_sum_assignment

            rows, cols = linear_sum_assignment(cost)
            pairs = list(zip(rows.tolist(), cols.tolist()))
        except Exception:  # scipy 缺失时退化为贪心匹配
            pairs = self._greedy(cost)

        matches: list[tuple[int, int]] = []
        used_dets: set[int] = set()
        for r, c in pairs:
            if cost[r, c] > 1.05:      # 代价过高 -> 不关联
                continue
            matches.append((tids[r], c))
            used_dets.add(c)

        unmatched = [i for i in range(len(detections)) if i not in used_dets]
        return matches, unmatched

    @staticmethod
    def _greedy(cost: np.ndarray) -> list[tuple[int, int]]:
        pairs: list[tuple[int, int]] = []
        used_rows: set[int] = set()
        used_cols: set[int] = set()
        flat = np.argsort(cost, axis=None)
        for idx in flat:
            r, c = divmod(int(idx), cost.shape[1])
            if r in used_rows or c in used_cols:
                continue
            used_rows.add(r)
            used_cols.add(c)
            pairs.append((r, c))
        return pairs

    # ---------- 轨迹维护 ----------
    def _spawn(self, det: Detection, t: float) -> None:
        tid = self._next_id
        self._next_id += 1
        tr = Track(tid=tid, bbox=det.bbox.copy(), kpts=None if det.kpts is None else det.kpts.copy(),
                   conf=det.conf, created_at=t, updated_at=t)
        tr.push(t, tr.bbox, tr.kpts)
        self.tracks[tid] = tr

    def _update_track(self, tr: Track, det: Detection, t: float) -> None:
        # 速度用 EMA 平滑，抑制关键点抖动带来的假速度。
        #
        # 单位必须是"每帧位移"，不能写成 (new_c - prev_c) / dt —— 那是"每秒位移"，
        # 而 _predict() 是 tr.bbox + vel 直接外推一帧。两者量纲不一致时（12fps 下差 12 倍），
        # 预测框会被推到画面外，IoU 骤降 → 频繁掉轨、ID 不停更换 → 所有时序行为判定失真。
        prev_c = tr.center
        raw = det.center - prev_c
        old = self._vel.get(tr.tid, raw)
        self._vel[tr.tid] = self.ema_alpha * raw + (1 - self.ema_alpha) * old

        tr.bbox = det.bbox.copy()
        tr.kpts = None if det.kpts is None else det.kpts.copy()
        tr.conf = det.conf
        tr.hits += 1
        tr.misses = 0
        tr.updated_at = t
        tr.push(t, tr.bbox, tr.kpts)

    def velocity(self, tid: int) -> np.ndarray | None:
        return self._vel.get(tid)
