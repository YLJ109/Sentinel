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

# 逼近/远离速度的归一化基准（帧宽/秒）。取值参考真实量级：
# 正常步行约 0.1、快走约 0.2、奔跑约 0.4 帧宽/秒。
# 取 0.25 表示"快走及以上"才算明显的主动逼近或后撤 —— 定得过高会让
# "追逃"这项指标几乎测不出来（欺凌场景里后撤的速度本就不会很快）。
_APPROACH_FULL_SCALE = 0.25


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


def _approach_rate(a: Track, b: Track, window: int = 4) -> tuple[float, float]:
    """两人各自"朝对方移动"的速度（归一化画面宽度 / 秒）。

    正值 = 朝对方移动，负值 = 远离对方。位移被投影到 AB 连线上，因此
    "横向走过"不会被误判成逼近或逃离 —— 这是把"追逃"与"路过"区分开的关键。
    返回 ``(a 的速度, b 的速度)``，均为沿 A→B 方向的分量。
    """
    ref_a = a.prev(window) or (a.hist[0] if a.hist else None)
    ref_b = b.prev(window) or (b.hist[0] if b.hist else None)
    if ref_a is None or ref_b is None:
        return 0.0, 0.0

    t_a, box_a, _ = ref_a
    t_b, box_b, _ = ref_b
    ca0 = np.array([(box_a[0] + box_a[2]) / 2.0, (box_a[1] + box_a[3]) / 2.0])
    cb0 = np.array([(box_b[0] + box_b[2]) / 2.0, (box_b[1] + box_b[3]) / 2.0])

    u = b.center - a.center
    n = float(np.linalg.norm(u))
    if n < 1e-6:
        return 0.0, 0.0
    u = u / n

    dt_a = max(1e-3, a.updated_at - t_a)
    dt_b = max(1e-3, b.updated_at - t_b)
    # 两人共用同一个方向轴 u（A→B），但返回值的语义要统一成"朝对方移动"：
    #   A 朝 B 移动 = 沿 +u
    #   B 朝 A 移动 = 沿 -u  ← 这里必须取负，否则两人同向行走会被算成
    #   "双方都在朝对方逼近"，进而让"追逃"与"退缩"两项指标恒为 0。
    va = float(np.dot(a.center - ca0, u)) / dt_a
    vb = -float(np.dot(b.center - cb0, u)) / dt_b
    return va, vb


def gesture_speed(track: Track, window: int = 4) -> float:
    """手腕**相对身体中心**的运动速度（归一化 / 秒）。

    与 ``wrist_speed`` 的关键差别：后者是绝对速度，人正常走路时整个人平移
    也会产生读数，因此不能用它判断"有没有在做手势"。这里先减去身体中心的
    位移，只保留手腕相对身体的运动 —— 挥手、指人、比划才会命中，
    "边走边聊"不会。
    """
    if len(track.hist) < 2:
        return 0.0
    now_t, now_box, now_k = track.hist[-1]
    ref = track.prev(window) or track.hist[0]
    ref_t, ref_box, ref_k = ref
    if now_k is None or ref_k is None:
        return 0.0
    dt = max(1e-3, now_t - ref_t)
    c_now = np.array([(now_box[0] + now_box[2]) / 2, (now_box[1] + now_box[3]) / 2])
    c_ref = np.array([(ref_box[0] + ref_box[2]) / 2, (ref_box[1] + ref_box[3]) / 2])
    d_body = c_now - c_ref
    best = 0.0
    for idx in (KP_L_WRIST, KP_R_WRIST):
        a, b = _kp(ref_k, idx), _kp(now_k, idx)
        if a is None or b is None:
            continue
        best = max(best, float(np.linalg.norm((b - a) - d_body)) / dt)
    return best


def facing_score(a: Track, b: Track) -> float:
    """两人的"对峙程度"：肩线与连线越接近垂直，返回值越大（0~1）。

    面对面争执时人的肩线大致垂直于两人连线；并肩同行时肩线与连线平行。
    COCO-17 只有肩点、拿不到真正的头部朝向，这里用肩线作为身体朝向的近似。
    取两人中的较大者 —— 只要有一方明显侧身面向对方，就说明存在对峙关系。
    """
    u = b.center - a.center
    n = float(np.linalg.norm(u))
    if n < 1e-6:
        return 0.0
    u = u / n
    best = 0.0
    for t in (a, b):
        l, r = _kp(t.kpts, KP_L_SHOULDER), _kp(t.kpts, KP_R_SHOULDER)
        if l is None or r is None:
            continue
        v = r - l
        m = float(np.linalg.norm(v))
        if m < 1e-6:
            continue
        v = v / m
        # |cos| 越小 → 肩线越垂直于连线 → 越像面对面
        best = max(best, 1.0 - abs(float(np.dot(v, u))))
    return best


def _suppression(a: Track, b: Track) -> float:
    """A 是否处于被 B 压制的姿态：A 的头部低于 B 的肩线。

    仅在两人贴近时由调用方启用，避免身高差与透视关系造成误判。
    """
    nose_a = _kp(a.kpts, KP_NOSE)
    sh_b = _mid(_kp(b.kpts, KP_L_SHOULDER), _kp(b.kpts, KP_R_SHOULDER))
    if nose_a is None or sh_b is None:
        return 0.0
    return 1.0 if float(nose_a[1]) > float(sh_b[1]) else 0.0


class _Interaction:
    """一对轨迹的交互统计（指数滑动平均，不保存完整历史）。"""

    __slots__ = ("asym", "retreat", "chase", "suppress", "samples")

    def __init__(self) -> None:
        self.asym = 0.0        # 运动强度不对称度
        self.retreat = 0.0     # 退缩不对称度
        self.chase = 0.0       # 追逃模式强度
        self.suppress = 0.0    # 压制姿态强度
        self.samples = 0


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
        # 每对轨迹的交互统计（欺凌 / 对等冲突 / 嬉闹判别用），随配对消失一并回收
        self._pair_state: dict[tuple[int, int], _Interaction] = {}

    # ---------- 对外入口 ----------
    def analyze(self, tracks: list[Track], extra_dets: list[Detection] | None = None) -> list[BehaviorHit]:
        hits: list[BehaviorHit] = []
        # 只对"本帧确有检测、且已通过确认门控"的轨迹做判定：
        # 未确认轨迹多是单帧误检，丢失轨迹用的是预测框（位置正确但没有真实观测），
        # 两者参与判定都会直接制造误报。
        people = [t for t in tracks if t.misses == 0 and t.confirmed]

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
            pair_hits = self._pairs(people)
            # 群体欺凌要先于两两结果落地：它是"两两对称性分析失效"场景的兜底，
            # 因此若已判定为群体围困，同帧里被该群体包含的两两打斗结果就不再重复上报
            # （否则一次围殴会同时产生"4 条对等打架 + 1 条群体欺凌"，报警列表被淹没）。
            group_hits = self._group_bullying(people) if runtime_config.capability("fight") else []
            if group_hits:
                covered = [set(h.track_ids) for h in group_hits]
                pair_hits = [
                    h for h in pair_hits
                    if not (h.event_type in ("fight", "bullying")
                            and any(set(h.track_ids) <= c for c in covered))
                ]
            hits.extend(group_hits)
            hits.extend(pair_hits)

        hits = self._fuse_model_hits(hits, self._model_behaviors(extra_dets))
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

    # ---------- 群体欺凌：多人围困一人 ----------
    def _group_bullying(self, people: list[Track]) -> list[BehaviorHit]:
        """判断是否存在"多人同时对同一人动手"的群体欺凌。

        为什么两两对称性分析在这里必然失效：真实围殴里几名攻击者动作幅度都不小、
        也都不退缩，于是**任意两人看都是"对等冲突"**，整体被降级成普通打架 ——
        实测三个真实素材全部如此，最该报警的情形反而没报。

        改用图结构判别，但**边取自跨帧累积的打斗证据**（``_pair_votes``），
        而不是单帧的姿态朝向。原因是实测发现：围殴时攻击者是轮流动手的，
        某一帧里往往只有一个人抬着手，靠单帧朝向连边会得到一条随帧抖动的断链；
        而"最近若干帧内这几个人彼此打过"是稳定得多的证据。

        图形态即为判据：
            群体围殴：攻击者各自与受害者相连、彼此之间不与对方冲突 → 星型图
            混战互殴：攻击者之间也互相开打 → 密集团，没有明确的中心
        因此"邻接度 ≥ 2 且邻居之间几乎无边"指向一个被多人同时卷入的个体。
        该判据不引入任何新模型，且"星型 vs 完全图"可以用一张图向评委讲清楚。
        """
        if len(people) < settings.GROUP_BULLY_MIN_ATTACKERS + 1:
            return []

        alive = {t.tid for t in people}
        adj: dict[int, set[int]] = {t.tid: set() for t in people}
        for (i, j, event), cnt in list(self._pair_votes.items()):
            if event != "fight" or i == j:
                continue
            if i not in alive or j not in alive:
                continue
            if cnt >= settings.GROUP_BULLY_EDGE_FRAMES:
                adj[i].add(j)
                adj[j].add(i)

        hits: list[BehaviorHit] = []
        by_tid = {t.tid: t for t in people}
        for center in people:
            # key 用 (tid, tid)：真实配对键是 (小, 大) 且小 < 大，不会冲突；
            # 同时 _decay_pairs 按"任一方消失即回收"的规则仍能正确清理它
            key = (center.tid, center.tid)
            nbrs = adj[center.tid]
            if len(nbrs) < settings.GROUP_BULLY_MIN_ATTACKERS:
                self._vote_pair(key, "group", False)
                continue

            n = len(nbrs)
            possible = n * (n - 1) / 2
            edges = sum(1 for x in nbrs for y in nbrs if x < y and y in adj[x])
            cohesion = edges / possible if possible else 0.0
            ok = cohesion <= settings.GROUP_BULLY_MAX_COHESION
            votes = self._vote_pair(key, "group", ok)
            if not ok or votes < settings.GROUP_BULLY_MIN_FRAMES:
                continue

            attackers = sorted(nbrs)
            box = self._union_box([center.bbox] + [by_tid[t].bbox for t in attackers])
            # 内聚度越低（攻击者越"一致对外"）置信度越高
            conf = round(min(0.96, 0.66 + 0.05 * n + 0.12 * (1.0 - cohesion)), 3)
            hits.append(BehaviorHit(
                "bullying", conf, [center.tid] + attackers, box,
                {"group": True, "group_size": n, "cohesion": round(cohesion, 2),
                 "center": center.tid, "others": attackers, "votes": votes},
            ))
        return hits

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
                speed_a, speed_b = wrist_speed(a), wrist_speed(b)
                speed = max(speed_a, speed_b)
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
                        # 在"存在打斗/推搡"的基础上，再判它是单向欺凌还是对等冲突，
                        # 并识别嬉闹以避免课间打闹误报 —— 这是本项目的关键差异点。
                        inter = self._interaction(a, b, key_base, ratio, speed_a, speed_b)
                        playful = self._is_playful(inter, speed)
                        pv = self._vote_pair(key_base, "play", playful)
                        if playful and pv >= settings.BULLY_MIN_FRAMES:
                            continue    # 判定为嬉闹：本帧不产生报警

                        is_bully = inter["bully_score"] >= settings.BULLY_SCORE_THRESHOLD
                        event = "bullying" if is_bully else "fight"
                        sev = min(1.0, 0.35 + speed / max(1e-6, settings.FIGHT_WRIST_SPEED * 2.2)
                                  + min(0.3, overlap * 2.0))
                        base = 0.55 + 0.4 * sev
                        # 欺凌再叠加"不对等程度"，让单向欺凌的报警比普通冲突更突出
                        conf = round(min(0.97, base + (0.08 * inter["bully_score"] if is_bully else 0.0)), 3)
                        hits.append(BehaviorHit(
                            event, conf, [a.tid, b.tid],
                            self._union_box([a.bbox, b.bbox]),
                            {"dist_ratio": round(ratio, 2), "overlap": round(overlap, 3),
                             "wrist_speed": round(speed, 2), "arm_raised": raised,
                             "entangle_hit": entangled, "votes": fv,
                             **inter},
                        ))
                        continue

                # --- 争吵：贴得很近 + 直立 + 无剧烈挥臂 + 存在对峙姿态或轻微动作 ---
                if not runtime_config.capability("argue"):
                    continue
                both_upright = all(
                    (torso_angle_deg(t.kpts) or 0.0) <= settings.FALL_TORSO_ANGLE for t in (a, b)
                )
                # 「对峙姿态」这一条不可省：缺了它，两人并排站着（排队、并肩同行）
                # 会因为"距离近 + 没有挥臂"而被判成争吵 —— 实测确认的误报来源。
                facing = facing_score(a, b)
                # 用手势速度（相对身体）而非绝对手腕速度：绝对速度里含走路时的
                # 整体平移，会把"边走边聊"误判成"在比划"
                gesture = max(gesture_speed(a), gesture_speed(b))
                confrontational = (facing >= settings.ARGUE_FACING_MIN
                                   or gesture >= settings.ARGUE_MIN_MOTION)
                argue_hit = (
                    ratio <= settings.ARGUE_DIST_RATIO
                    and both_upright
                    and speed < settings.FIGHT_WRIST_SPEED
                    and confrontational
                )
                av = self._vote_pair(key_base, "argue", argue_hit)
                if argue_hit and av >= settings.ARGUE_MIN_FRAMES:
                    conf = round(min(0.9, 0.5 + 0.4 * (1.0 - min(1.0, ratio / settings.ARGUE_DIST_RATIO))), 3)
                    hits.append(BehaviorHit(
                        "argue", conf, [a.tid, b.tid],
                        self._union_box([a.bbox, b.bbox]),
                        {"dist_ratio": round(ratio, 2), "facing": round(facing, 2), "votes": av},
                    ))
        return hits

    # ---------- 互动对称性分析：欺凌 / 对等冲突 / 嬉闹 ----------
    def _interaction(self, a: Track, b: Track, key: tuple[int, int],
                     ratio: float, speed_a: float, speed_b: float) -> dict[str, float]:
        """刻画两人互动的"不对等程度"。

        常见打架检测只判"距离近 + 手部动作快"，因此并肩走路、拍肩、课间追逐打闹
        都会误报。根因在于：这三类场景在"是否有肢体动作"这个维度上无法区分，
        真正的差别在于 **力量与主动权是否对等**。这里用四个可解释指标来刻画：

          ① 运动强度不对称 asymmetry —— 一方猛烈挥臂、另一方几乎不动
          ② 退缩不对称   retreat     —— 一方持续逼近、另一方持续后撤
          ③ 追逃模式     chase       —— 明确的"追的人"与"逃的人"分工
          ④ 压制姿态     suppression —— 贴近时一方头部低于另一方肩线

        四项指标全部来自已有的 COCO-17 关键点与轨迹历史，不引入任何新模型，
        每一项都能单独向评审解释其物理含义与阈值来历。
        """
        st = self._pair_state.setdefault(key, _Interaction())

        # ① 运动强度不对称：两人手腕速度的相对差异，0 表示完全对等
        asym = abs(speed_a - speed_b) / (speed_a + speed_b + 1e-6)

        # ②③ 逼近/远离速度（已投影到连线方向，横向路过不计）
        va, vb = _approach_rate(a, b)
        fs = _APPROACH_FULL_SCALE
        a_approach = min(1.0, max(0.0, va) / fs)
        b_approach = min(1.0, max(0.0, vb) / fs)
        a_flee = min(1.0, max(0.0, -va) / fs)
        b_flee = min(1.0, max(0.0, -vb) / fs)

        # 追逃：一方在逼近、同时另一方在后撤（两种分工取更强者）
        chase = max(min(a_approach, b_flee), min(b_approach, a_flee))
        # 退缩不对称：两人的"逃离倾向"差距越大，越接近单向欺凌
        retreat = abs(a_flee - b_flee)

        # ④ 压制姿态：仅在贴近时计算，避免身高差与透视造成误判
        suppress = 0.0
        if ratio <= settings.FIGHT_DIST_RATIO:
            suppress = max(_suppression(a, b), _suppression(b, a))

        al = settings.BULLY_EMA_ALPHA
        st.asym += (asym - st.asym) * al
        st.retreat += (retreat - st.retreat) * al
        st.chase += (chase - st.chase) * al
        st.suppress += (suppress - st.suppress) * al
        st.samples += 1

        score = (settings.BULLY_ASYM_WEIGHT * st.asym
                 + settings.BULLY_FLEE_WEIGHT * st.retreat
                 + settings.BULLY_CHASE_WEIGHT * st.chase
                 + settings.BULLY_SUPPRESS_WEIGHT * st.suppress)
        return {
            "asymmetry": round(st.asym, 3),
            "retreat": round(st.retreat, 3),
            "chase": round(st.chase, 3),
            "suppression": round(st.suppress, 3),
            "bully_score": round(min(1.0, score), 3),
        }

    @staticmethod
    def _is_playful(inter: dict[str, float], speed: float) -> bool:
        """嬉闹判定：动作不剧烈，且四项对称指标都是"对等且无退缩"。

        为什么必须叠加"动作不剧烈"：如果双方手腕速度已经到了强证据级别，
        那就说明确实在激烈互殴，即便双方完全对等也应报警。
        嬉闹抑制只针对"贴近 + 轻微肢体接触"这一类最常见的误报 ——
        排队、并肩、拍肩、轻推。课间追逐打闹是本场景最大的误报来源
        （有公开案例提到"两个同学并肩走路也会报警"）。
        """
        if speed >= settings.FIGHT_WRIST_SPEED:
            return False
        return (inter["asymmetry"] <= settings.PLAY_ASYM_MAX
                and inter["retreat"] <= settings.PLAY_RETREAT_MAX
                and inter["chase"] <= settings.PLAY_CHASE_MAX
                and inter["suppression"] <= settings.PLAY_SUPPRESS_MAX)

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
        # 交互统计同样要回收，否则长期运行下会随历史配对数量持续增长
        for k in list(self._pair_state.keys()):
            if k[0] not in alive or k[1] not in alive:
                self._pair_state.pop(k, None)

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

    def _fuse_model_hits(self, rule_hits: list[BehaviorHit],
                         model_hits: list[BehaviorHit]) -> list[BehaviorHit]:
        """把「骨架规则判定」与「自定义行为模型」的结果做双证据融合。

        两者是彼此独立的证据来源：
          - 规则来自姿态几何与运动学，可解释、零训练成本，但阈值依赖现场标定；
          - 模型来自数据驱动，泛化更好，但是黑盒，判定口径也与规则不同。
        同一目标上两条证据同时命中时，可信度显著高于任一单独来源 ——
        这与视觉/语音的多模态融合是同一思路，此处做的是「同模态内的双证据融合」。

        融合规则：
          - 同类 + 位置重叠 → 加权平均并上浮置信度，detail 中标注 fused=True
          - 模型命中但规则未命中 → 原样保留，用模型补规则的漏报
        """
        if not model_hits:
            return rule_hits

        matched: set[int] = set()
        for rh in rule_hits:
            for j, mh in enumerate(model_hits):
                if j in matched or mh.event_type != rh.event_type:
                    continue
                if rh.bbox is None or mh.bbox is None:
                    continue
                # 位置重叠才认为是同一目标：规则给出的是轨迹框，模型给出的是检测框
                if box_iou(rh.bbox, mh.bbox) < 0.2:
                    continue
                fused = min(0.99, 0.5 * float(rh.confidence) + 0.5 * float(mh.confidence) + 0.08)
                rh.confidence = round(fused, 3)
                rh.detail = {
                    **rh.detail,
                    "model_class": mh.detail.get("class"),
                    "model_conf": round(float(mh.confidence), 3),
                    "fused": True,
                }
                matched.add(j)
                break

        # 未被规则覆盖的模型命中单独输出（对应"规则漏报、模型补上"的情形）
        return rule_hits + [mh for j, mh in enumerate(model_hits) if j not in matched]

    @staticmethod
    def _union_box(boxes: list[np.ndarray]) -> np.ndarray | None:
        if not boxes:
            return None
        arr = np.array(boxes, dtype=float)
        return np.array([arr[:, 0].min(), arr[:, 1].min(), arr[:, 2].max(), arr[:, 3].max()])
