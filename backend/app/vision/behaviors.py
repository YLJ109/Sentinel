"""行为识别算法：基于真实姿态关键点与轨迹时序特征的几何/运动学判定。

所有判定都不是"随机模拟"，而是对 YOLOv8-Pose 输出的 17 个 COCO 关键点做：
- 静态几何（躯干倾角、外接框长宽比、手腕-鼻尖距离、双人中心距）
- 动态运动学（手腕归一化速度、肢体纠缠 IoU）
- 时序去抖（轨迹级滑窗投票，避免单帧误报）

判定阈值集中在 ``core.config``，可按现场摄像头视角标定。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.config import settings
from app.core.runtime_config import runtime_config
from app.vision.types import (
    KP_L_ELBOW, KP_L_HIP, KP_L_SHOULDER, KP_L_WRIST, KP_NOSE,
    KP_R_ELBOW, KP_R_HIP, KP_R_SHOULDER, KP_R_WRIST,
    Detection, Track,
)

KP_CONF_MIN = 0.30  # 关键点置信度门限


def _kp(kpts: np.ndarray | None, idx: int) -> np.ndarray | None:
    """取一个可信关键点坐标。"""
    if kpts is None or idx >= len(kpts):
        return None
    x, y, c = kpts[idx][:3]
    if c < KP_CONF_MIN or (x <= 0 and y <= 0):
        return None
    return np.array([float(x), float(y)])


def _mid(a: np.ndarray | None, b: np.ndarray | None) -> np.ndarray | None:
    if a is None or b is None:
        return None
    return (a + b) / 2


def torso_angle_deg(kpts: np.ndarray | None) -> float | None:
    """躯干（肩中点→胯中点）与竖直方向的夹角，0° 为直立、90° 为水平。"""
    sh = _mid(_kp(kpts, KP_L_SHOULDER), _kp(kpts, KP_R_SHOULDER))
    hip = _mid(_kp(kpts, KP_L_HIP), _kp(kpts, KP_R_HIP))
    if sh is None or hip is None:
        return None
    v = hip - sh
    if np.linalg.norm(v) < 1e-6:
        return None
    angle = math.degrees(math.atan2(abs(float(v[0])), abs(float(v[1]))))
    return angle


def shoulder_width(kpts: np.ndarray | None) -> float | None:
    a, b = _kp(kpts, KP_L_SHOULDER), _kp(kpts, KP_R_SHOULDER)
    if a is None or b is None:
        return None
    w = float(np.linalg.norm(a - b))
    return w if w > 1e-3 else None


def wrist_speed(track: Track, window: int = 4) -> float:
    """手腕归一化速度（帧宽/秒），取左右手最大值。"""
    if len(track.hist) < 2:
        return 0.0
    now_t, _, now_k = track.hist[-1]
    ref = track.prev(window) or track.hist[0]
    ref_t, _, ref_k = ref
    dt = max(1e-3, now_t - ref_t)
    best = 0.0
    for idx in (KP_L_WRIST, KP_R_WRIST):
        a, b = _kp(ref_k, idx), _kp(now_k, idx)
        if a is None or b is None:
            continue
        best = max(best, float(np.linalg.norm(b - a)) / dt)
    return best


def arm_raised(kpts: np.ndarray | None) -> bool:
    """手腕是否高于肩部。

    用于识别出拳/推搡/抓扯等攻击性姿态。注意不能用"腕-肩距离/肩宽"来判断：
    手臂自然下垂时该比值约为 2.2~2.6，会把正常站立的人误判成打架；
    而"手腕高于肩"在自然下垂时恒为假，是更干净的判别量。
    """
    for sh_i, wr_i in ((KP_L_SHOULDER, KP_L_WRIST), (KP_R_SHOULDER, KP_R_WRIST)):
        sh, wr = _kp(kpts, sh_i), _kp(kpts, wr_i)
        if sh is not None and wr is not None and wr[1] < sh[1] - 0.02:
            return True
    return False


def box_iou(a: np.ndarray, b: np.ndarray) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    aa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    bb = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return float(inter / (aa + bb - inter + 1e-9))


@dataclass
class BehaviorHit:
    """一次行为判定结果。"""

    event_type: str
    confidence: float
    track_ids: list[int]
    bbox: np.ndarray | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class BehaviorAnalyzer:
    """单路视频的行为分析器（持有该路的投票状态）。"""

    def __init__(self) -> None:
        self._pair_votes: dict[tuple[int, int, str], int] = {}

    # ---------- 对外入口 ----------
    def analyze(self, tracks: list[Track], extra_dets: list[Detection] | None = None) -> list[BehaviorHit]:
        hits: list[BehaviorHit] = []
        people = [t for t in tracks if t.misses == 0]

        if runtime_config.capability("crowd"):
            hits.extend(self._crowd(people))

        for tr in people:
            if runtime_config.capability("fall"):
                hit = self._fall(tr)
                if hit:
                    hits.append(hit)
                    continue  # 已倒地，不再判吸烟
            if runtime_config.capability("smoke"):
                smoke = self._smoke(tr)
                if smoke:
                    hits.append(smoke)

        if runtime_config.capability("fight") or runtime_config.capability("argue"):
            hits.extend(self._pairs(people))

        hits.extend(self._model_behaviors(extra_dets))
        self._decay_pairs(people)
        return hits

    # ---------- 人员聚集 ----------
    def _crowd(self, people: list[Track]) -> list[BehaviorHit]:
        n = len(people)
        if n < settings.CROWD_MIN_PEOPLE:
            return []
        # 置信度随人数增长，封顶 0.95
        conf = min(0.95, 0.5 + 0.08 * (n - settings.CROWD_MIN_PEOPLE + 1))
        box = self._union_box([t.bbox for t in people])
        return [BehaviorHit("crowd", round(conf, 3), [t.tid for t in people], box,
                            {"people": n})]

    # ---------- 跌倒 ----------
    def _fall(self, tr: Track) -> BehaviorHit | None:
        aspect = float(tr.bbox[2] - tr.bbox[0]) / max(1e-6, float(tr.bbox[3] - tr.bbox[1]))
        angle = torso_angle_deg(tr.kpts)
        nose = _kp(tr.kpts, KP_NOSE)
        hip_mid = _mid(_kp(tr.kpts, KP_L_HIP), _kp(tr.kpts, KP_R_HIP))
        sw = shoulder_width(tr.kpts)

        # 三个独立证据
        e_aspect = aspect >= settings.FALL_ASPECT_RATIO
        e_angle = angle is not None and angle >= settings.FALL_TORSO_ANGLE
        e_head = (
            nose is not None and hip_mid is not None and sw is not None
            and (hip_mid[1] - nose[1]) < settings.FALL_HEAD_DROP * sw
        )

        # 无姿态时仅靠长宽比（保守：要求明显横向，且单独走一条判定分支，
        # 否则 evidence 上限为 1 会永远达不到 >=2 的门槛而静默失效）
        if tr.kpts is None:
            prone = aspect >= settings.FALL_ASPECT_RATIO + 0.35
            votes = tr.vote("fall", prone, settings.EVENT_VOTE_WINDOW)
            if not prone or votes < settings.FALL_MIN_FRAMES:
                return None
            conf = min(0.85, 0.55 + 0.03 * min(6, votes - settings.FALL_MIN_FRAMES))
            return BehaviorHit(
                "fall", round(conf, 3), [tr.tid], tr.bbox.copy(),
                {"aspect": round(aspect, 2), "evidence": 1, "pose_missing": True, "votes": votes},
            )

        evidence = int(e_aspect) + int(e_angle) + int(e_head)
        score = {0: 0.0, 1: 0.45, 2: 0.72, 3: 0.9}[evidence]

        hit = evidence >= 2
        votes = tr.vote("fall", hit, settings.EVENT_VOTE_WINDOW)
        if not hit or votes < settings.FALL_MIN_FRAMES:
            return None
        conf = min(0.97, score + 0.04 * min(4, votes - settings.FALL_MIN_FRAMES))
        return BehaviorHit(
            "fall", round(conf, 3), [tr.tid], tr.bbox.copy(),
            {"aspect": round(aspect, 2), "torso_angle": None if angle is None else round(angle, 1),
             "evidence": evidence, "votes": votes},
        )

    # ---------- 疑似吸烟 ----------
    def _smoke(self, tr: Track) -> BehaviorHit | None:
        sw = shoulder_width(tr.kpts)
        nose = _kp(tr.kpts, KP_NOSE)
        if sw is None or nose is None:
            tr.vote("smoke", False, settings.EVENT_VOTE_WINDOW)
            return None
        angle = torso_angle_deg(tr.kpts)
        if angle is not None and angle > settings.FALL_TORSO_ANGLE:
            tr.vote("smoke", False, settings.EVENT_VOTE_WINDOW)
            return None

        best, best_side = None, ""
        for idx, side in ((KP_L_WRIST, "left"), (KP_R_WRIST, "right")):
            wr = _kp(tr.kpts, idx)
            if wr is None:
                continue
            d = float(np.linalg.norm(wr - nose)) / sw
            if best is None or d < best:
                best, best_side = d, side
        if best is None:
            tr.vote("smoke", False, settings.EVENT_VOTE_WINDOW)
            return None

        # 手腕贴近口鼻，且手臂处于抬起状态（肘高于胯）
        elbow_up = False
        for el_i, hp_i in ((KP_L_ELBOW, KP_L_HIP), (KP_R_ELBOW, KP_R_HIP)):
            el, hp = _kp(tr.kpts, el_i), _kp(tr.kpts, hp_i)
            if el is not None and hp is not None and el[1] < hp[1]:
                elbow_up = True
                break

        hit = best <= settings.SMOKE_HAND_HEAD_RATIO and elbow_up
        votes = tr.vote("smoke", hit, settings.EVENT_VOTE_WINDOW)
        if not hit or votes < settings.SMOKE_MIN_FRAMES:
            return None
        closeness = 1.0 - min(1.0, best / max(1e-6, settings.SMOKE_HAND_HEAD_RATIO))
        conf = min(0.9, 0.5 + 0.4 * closeness)
        return BehaviorHit(
            "smoke", round(conf, 3), [tr.tid], tr.bbox.copy(),
            {"hand_head_ratio": round(best, 3), "hand": best_side, "votes": votes},
        )

    # ---------- 双人：打架 / 争吵 ----------
    def _pairs(self, people: list[Track]) -> list[BehaviorHit]:
        hits: list[BehaviorHit] = []
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                a, b = people[i], people[j]
                key_base = (min(a.tid, b.tid), max(a.tid, b.tid))

                dist = float(np.linalg.norm(a.center - b.center))
                mean_h = (a.height + b.height) / 2
                ratio = dist / max(1e-6, mean_h)
                overlap = box_iou(a.bbox, b.bbox)
                speed = max(wrist_speed(a), wrist_speed(b))
                raised = arm_raised(a.kpts) or arm_raised(b.kpts)

                # --- 打架判定 ---
                # 强证据（单独成立即可）：快速挥臂、抬手攻击姿态。
                # 弱证据：外接框交叠。交叠本身不足以判定打架——排队、并排站立、
                # 拥挤时人体框大量交叠但并无冲突，因此交叠必须叠加一定幅度的
                # 肢体运动才升级为打架（对应真实的扭打/推搡）。
                if runtime_config.capability("fight"):
                    strong = speed >= settings.FIGHT_WRIST_SPEED or raised
                    entangled = (overlap >= settings.FIGHT_IOU_OVERLAP
                                 and speed >= settings.FIGHT_WRIST_SPEED * 0.35)
                    fight_hit = ratio <= settings.FIGHT_DIST_RATIO and (strong or entangled)

                    fv = self._vote_pair(key_base, "fight", fight_hit)
                    if fight_hit and fv >= settings.FIGHT_MIN_FRAMES:
                        sev = min(1.0, 0.35 + speed / max(1e-6, settings.FIGHT_WRIST_SPEED * 2.2)
                                  + min(0.3, overlap * 2.0))
                        conf = round(min(0.97, 0.55 + 0.4 * sev), 3)
                        hits.append(BehaviorHit(
                            "fight", conf, [a.tid, b.tid],
                            self._union_box([a.bbox, b.bbox]),
                            {"dist_ratio": round(ratio, 2), "overlap": round(overlap, 3),
                             "wrist_speed": round(speed, 2), "arm_raised": raised,
                             "entangle_hit": entangled, "votes": fv},
                        ))
                        continue

                # --- 争吵：持续贴近、直立、无剧烈挥臂 ---
                if not runtime_config.capability("argue"):
                    continue
                both_upright = all(
                    (torso_angle_deg(t.kpts) or 0.0) <= settings.FALL_TORSO_ANGLE for t in (a, b)
                )
                argue_hit = (
                    ratio <= settings.ARGUE_DIST_RATIO
                    and both_upright
                    and speed < settings.FIGHT_WRIST_SPEED
                )
                av = self._vote_pair(key_base, "argue", argue_hit)
                if argue_hit and av >= settings.ARGUE_MIN_FRAMES:
                    conf = round(min(0.9, 0.5 + 0.4 * (1.0 - min(1.0, ratio / settings.ARGUE_DIST_RATIO))), 3)
                    hits.append(BehaviorHit(
                        "argue", conf, [a.tid, b.tid],
                        self._union_box([a.bbox, b.bbox]),
                        {"dist_ratio": round(ratio, 2), "votes": av},
                    ))
        return hits

    def _vote_pair(self, key: tuple[int, int], event: str, hit: bool) -> int:
        k = (key[0], key[1], event)
        cur = self._pair_votes.get(k, 0)
        cur = min(settings.EVENT_VOTE_WINDOW, cur + 1) if hit else max(0, cur - 2)
        self._pair_votes[k] = cur
        return cur

    def _decay_pairs(self, people: list[Track]) -> None:
        """清理已消失轨迹的配对状态，防止内存增长。

        只要配对中**任意一方**已不在画面，这条配对就失去意义，应当回收
        （原来用 `and` 判断双方都已消失，导致"一人离开"的配对永久驻留）。
        """
        alive = {t.tid for t in people}
        for k in list(self._pair_votes.keys()):
            if k[0] not in alive or k[1] not in alive:
                self._pair_votes.pop(k, None)

    # ---------- 自定义行为模型（烟/暴力等）----------
    def _model_behaviors(self, dets: list[Detection] | None) -> list[BehaviorHit]:
        if not dets:
            return []
        from app.core.config import behavior_meta

        out: list[BehaviorHit] = []
        for d in dets:
            name = d.cls_name.lower()
            mapped = None
            if name in ("cigarette", "smoke", "smoking", "cigar"):
                mapped = "smoke"
            elif name in ("fight", "violence", "punch", "assault", "weapon"):
                mapped = "fight"
            elif name in ("fall", "fallen"):
                mapped = "fall"
            if not mapped:
                continue
            if d.conf < settings.ALARM_MIN_CONFIDENCE:
                continue
            out.append(BehaviorHit(
                mapped, round(float(d.conf), 3), [], d.bbox.copy(),
                {"source": "behavior_model", "class": d.cls_name,
                 "label": behavior_meta(mapped)["label"]},
            ))
        return out

    @staticmethod
    def _union_box(boxes: list[np.ndarray]) -> np.ndarray | None:
        if not boxes:
            return None
        arr = np.array(boxes, dtype=float)
        return np.array([arr[:, 0].min(), arr[:, 1].min(), arr[:, 2].max(), arr[:, 3].max()])
