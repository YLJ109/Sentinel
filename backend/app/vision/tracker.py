"""多目标跟踪：卡尔曼预测 + 两阶段关联 + 轨迹确认门控 + 时间化生命周期。

为什么不用 ultralytics 内置的 ByteTrack：
``model.track(persist=True)`` 的跟踪器状态挂在模型实例上，多路摄像头交替送帧会互相污染。
这里为每个摄像头维护独立跟踪器实例。

相对初版的四点改进（目标：少换 ID、不闪框）：

**1. 卡尔曼滤波取代 EMA 匀速外推。**
    初版对速度做指数平滑再线性外推，观测噪声与过程噪声不分，快速变向时预测框会甩出去。
    改用 8 维匀速卡尔曼（``[cx, cy, w, h, vx, vy, vw, vh]``），预测由状态协方差驱动，
    遮挡 1~2 帧时框会沿运动方向平滑延续，而不是跳到原地。

**2. 两阶段关联（借鉴 ByteTrack）。**
    高分框先用较宽的门限关联；剩下的轨迹再与**低分框**做一次更严格的关联。
    低分框通常来自遮挡、运动模糊，直接丢掉会让轨迹断掉、换 ID；拿来补关联则能续上，
    但又不允许低分框**新建**轨迹 —— 否则会产生没有对应实体的幽灵框。

**3. 轨迹确认门控。**
    新轨迹出生时 ``confirmed=False``，连续命中 ``TRACK_CONFIRM_HITS`` 次才对外可见。
    单帧误检（反光、海报上的人像）活不过门控，前端也就不会出现闪一下就消失的框。

**4. 生命周期时间化 + 外推上限。**
    初版按"丢失帧数"回收，帧率一变（实时 30fps 与视频文件 25fps）存活时长就跟着变。
    改为按 ``TRACK_TTL_SEC`` 时间回收；同时限制连续外推帧数，避免遮挡过久后
    预测框跑到画面外。

所有坐标均为 0~1 归一化，与 ``Detection`` / ``Track`` 保持一致。
"""
from __future__ import annotations

import logging

import numpy as np

from app.core.config import settings
from app.vision.types import Detection, Track

log = logging.getLogger("vision.tracker")

# 关联代价门限：代价 = (1 - IoU) + 距离惩罚 + 0.35 * 中心距
_COST_GATE_HIGH = 1.05   # 高分阶段：沿用初版门限，保证召回
_COST_GATE_LOW = 0.75    # 低分阶段：更严，只接受形状与位置都对得上的补关联
_MIN_SIZE = 1e-3         # 归一化宽高的下限，防止滤波把框压成一条线


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


def _to_cxcywh(bbox: np.ndarray) -> np.ndarray:
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    return np.array([(x1 + x2) / 2, (y1 + y2) / 2, max(_MIN_SIZE, x2 - x1), max(_MIN_SIZE, y2 - y1)])


def _to_xyxy(state: np.ndarray) -> np.ndarray:
    cx, cy, w, h = (float(v) for v in state[:4])
    w, h = max(_MIN_SIZE, w), max(_MIN_SIZE, h)
    return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])


class KalmanFilter:
    """8 维匀速卡尔曼滤波（纯 numpy，不引入 filterpy 依赖）。

    状态 ``x = [cx, cy, w, h, vx, vy, vw, vh]``，观测为 ``z = [cx, cy, w, h]``。
    过程噪声按 dt 缩放：dt 越大，对匀速假设的信任越低。
    """

    # 观测噪声：YOLO 的框抖动在归一化坐标下大约是千分之一量级
    _R = np.diag([1e-4, 1e-4, 1e-4, 1e-4])
    # 过程噪声（每单位 dt 的加速度方差）
    _Q_POS = 2e-4
    _Q_VEL = 5e-3

    def __init__(self, bbox: np.ndarray, t: float) -> None:
        self.x = np.zeros(8, dtype=np.float64)
        self.x[:4] = _to_cxcywh(bbox)
        # 对未观测过的速度给较大的初始不确定度，让滤波器尽快收敛到真实速度
        self.P = np.diag([1e-4, 1e-4, 1e-4, 1e-4, 1e-1, 1e-1, 1e-1, 1e-1]).astype(np.float64)
        self.last_t = t
        self.dt = 1.0 / 30.0

    def _F(self, dt: float) -> np.ndarray:
        f = np.eye(8)
        f[0, 4] = f[1, 5] = f[2, 6] = f[3, 7] = dt
        return f

    def _Q(self, dt: float) -> np.ndarray:
        q = np.zeros(8, dtype=np.float64)
        q[:4] = self._Q_POS * dt
        q[4:] = self._Q_VEL * dt
        return np.diag(q)

    def predict(self, t: float) -> np.ndarray:
        """推进到时刻 t 并返回预测 bbox（xyxy）。"""
        dt = float(np.clip(t - self.last_t, 1e-3, 0.5))
        self.dt = dt
        f = self._F(dt)
        self.x = f @ self.x
        self.P = f @ self.P @ f.T + self._Q(dt)
        self.last_t = t
        return _to_xyxy(self.x)

    def update(self, bbox: np.ndarray) -> None:
        """用观测框修正状态。"""
        h = np.zeros((4, 8), dtype=np.float64)
        h[:4, :4] = np.eye(4)
        z = _to_cxcywh(bbox)
        y = z - h @ self.x
        s = h @ self.P @ h.T + self._R
        try:
            k = self.P @ h.T @ np.linalg.inv(s)
        except np.linalg.LinAlgError:      # 协方差奇异：退化为直接采用观测
            self.x[:4] = z
            return
        self.x = self.x + k @ y
        i_kh = np.eye(8) - k @ h
        self.P = i_kh @ self.P @ i_kh.T + k @ self._R @ k.T   # Joseph 形式，保证对称正定
        self.x[2] = max(_MIN_SIZE, self.x[2])
        self.x[3] = max(_MIN_SIZE, self.x[3])

    @property
    def bbox(self) -> np.ndarray:
        return _to_xyxy(self.x)


class PeopleCounter:
    """区域内人数统计：中位数滤波 + 不对称迟滞。

    直接数"当前帧有几个框"会随着遮挡、误检上下跳，投到大屏上非常刺眼。
    这里做两级平滑：
      1. 对最近 ``window`` 帧的原始计数取**中位数**，滤掉单帧尖峰；
      2. 非对称迟滞 —— 人数**涨**要连续 ``up_hold`` 帧确认（新目标可能就是误检），
         人数**跌**要连续 ``down_hold`` 帧确认（短暂遮挡不该把人数打下去）。
    """

    def __init__(self, window: int | None = None, up_hold: int | None = None,
                 down_hold: int | None = None) -> None:
        # 参数默认跟随全局设置（可在系统设置页调整），显式传参则固定为该值
        self._window = window
        self._up_hold = up_hold
        self._down_hold = down_hold
        self._hist: list[int] = []
        self.value = 0
        self._up = 0
        self._down = 0

    def push(self, raw: int) -> int:
        raw = max(0, int(raw))
        window = self._window or settings.PEOPLE_WINDOW
        up_hold = self._up_hold or settings.PEOPLE_UP_HOLD
        down_hold = self._down_hold or settings.PEOPLE_DOWN_HOLD

        self._hist.append(raw)
        while len(self._hist) > max(1, window):
            self._hist.pop(0)
        med = int(np.median(self._hist)) if self._hist else raw

        if med > self.value:
            self._up += 1
            self._down = 0
            if self._up >= up_hold:
                self.value = med
                self._up = 0
        elif med < self.value:
            self._down += 1
            self._up = 0
            if self._down >= down_hold:
                self.value = med
                self._down = 0
        else:
            self._up = self._down = 0
        return self.value

    def reset(self) -> None:
        self._hist.clear()
        self.value = 0
        self._up = self._down = 0


class IoUTracker:
    """单路视频的多目标跟踪器（对外接口保持不变）。"""

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_miss: int | None = None,
        ema_alpha: float = 0.65,      # 保留形参以兼容旧调用方，新实现不再使用
        dist_gate: float = 0.28,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_miss = max_miss or settings.TRACK_TTL_FRAMES
        self.ema_alpha = ema_alpha
        self.dist_gate = dist_gate      # 中心距门限（归一化），超过视为不同目标
        self.tracks: dict[int, Track] = {}
        self._kf: dict[int, KalmanFilter] = {}
        self._next_id = 1

    # ---------- 主流程 ----------
    def update(self, detections: list[Detection], t: float) -> list[Track]:
        """用当前帧检测结果更新轨迹，返回本帧全部存活轨迹。

        门控与回收阈值直接读 ``settings``，这样设置页改完下一帧即生效，
        不需要重建管线。
        """
        ttl_sec = float(settings.TRACK_TTL_SEC)
        max_extrap = int(settings.TRACK_MAX_EXTRAP)
        max_miss = int(settings.TRACK_TTL_FRAMES)

        existing_ids = set(self.tracks.keys())
        preds = {tid: self._kf[tid].predict(t) for tid in self.tracks if tid in self._kf}
        for tid, tr in self.tracks.items():
            if tid not in preds:              # 兜底：滤波器缺失时沿用上一帧框
                preds[tid] = tr.bbox.copy()

        # 高分 / 低分分流。模型自身已按 DETECT_CONF 过滤，
        # 若阈值调得比 0.5 高，低分集合为空，第二阶段自然退化为空操作。
        high = [i for i, d in enumerate(detections) if d.conf >= 0.5]
        low = [i for i, d in enumerate(detections) if d.conf < 0.5]

        matches, rest_tids, used_dets = self._associate(preds, detections, high, _COST_GATE_HIGH)
        # 第二阶段：未被关联的轨迹 vs 低分框，门限更严格且不允许新建轨迹
        if rest_tids and low:
            free = [i for i in low if i not in used_dets]
            if free:
                again, _, used2 = self._associate(
                    {tid: preds[tid] for tid in rest_tids}, detections, free, _COST_GATE_LOW
                )
                matches.extend(again)
                used_dets |= used2

        for tid, di in matches:
            self._update_track(self.tracks[tid], detections[di], t)

        # 只有高分框可以新建轨迹：低分框多为遮挡/模糊的残影，
        # 让它开局会产生没有任何真实目标对应的幽灵框
        matched_ids = {tid for tid, _ in matches}
        for di in high:
            if di not in used_dets:
                self._spawn(detections[di], t)

        # 未匹配轨迹：用预测框延续（不闪框的关键），超时后回收
        for tid in existing_ids:
            if tid in matched_ids:
                continue
            tr = self.tracks.get(tid)
            if tr is None:
                continue
            tr.misses += 1
            if tr.misses <= max_extrap:
                tr.bbox = preds.get(tid, tr.bbox).copy()
            if (t - tr.updated_at) > ttl_sec or tr.misses > max_miss:
                self.tracks.pop(tid, None)
                self._kf.pop(tid, None)

        for tr in self.tracks.values():
            tr.age += 1
        return list(self.tracks.values())

    def reset(self) -> None:
        self.tracks.clear()
        self._kf.clear()
        self._next_id = 1

    # ---------- 关联 ----------
    def _associate(
        self,
        preds: dict[int, np.ndarray],
        detections: list[Detection],
        det_idx: list[int],
        gate: float,
    ) -> tuple[list[tuple[int, int]], list[int], set[int]]:
        """匈牙利匹配。返回 (匹配对, 未匹配的轨迹 id, 已占用的检测下标)。"""
        matched_tids = set()
        if not preds or not det_idx:
            return [], list(preds.keys()), set()

        tids = list(preds.keys())
        cost = np.zeros((len(tids), len(det_idx)), dtype=np.float64)
        for r, tid in enumerate(tids):
            pb = preds[tid]
            pc = np.array([(pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2])
            for c, di in enumerate(det_idx):
                det = detections[di]
                overlap = iou(pb, det.bbox)
                dist = float(np.linalg.norm(pc - det.center))
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
            if cost[r, c] > gate:      # 代价过高 -> 不关联
                continue
            matches.append((tids[r], det_idx[c]))
            matched_tids.add(tids[r])
            used_dets.add(det_idx[c])

        rest = [tid for tid in tids if tid not in matched_tids]
        return matches, rest, used_dets

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
                   conf=det.conf, created_at=t, updated_at=t, confirmed=False)
        tr.push(t, tr.bbox, tr.kpts)
        self.tracks[tid] = tr
        self._kf[tid] = KalmanFilter(det.bbox, t)

    def _update_track(self, tr: Track, det: Detection, t: float) -> None:
        kf = self._kf.get(tr.tid)
        if kf is None:
            kf = self._kf[tr.tid] = KalmanFilter(det.bbox, t)
        kf.update(det.bbox)

        tr.bbox = kf.bbox
        tr.kpts = None if det.kpts is None else det.kpts.copy()
        tr.conf = det.conf
        tr.hits += 1
        tr.misses = 0
        tr.updated_at = t
        if not tr.confirmed and tr.hits >= int(settings.TRACK_CONFIRM_HITS):
            # 连续命中达到门控值才对外可见，单帧误检不会漏到前端
            tr.confirmed = True
        tr.push(t, tr.bbox, tr.kpts)

    def velocity(self, tid: int) -> np.ndarray | None:
        """返回"每帧位移"的速度（与历史实现同量纲）。"""
        kf = self._kf.get(tid)
        if kf is None:
            return None
        return np.array([kf.x[4] * kf.dt, kf.x[5] * kf.dt])
