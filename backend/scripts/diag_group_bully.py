"""群体欺凌判据诊断：打印指定帧上的对峙关系图与各项原始数值。

调参时用它定位"到底卡在哪一条"，避免对着阈值盲猜。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.vision.behaviors import arm_raised, facing_score, wrist_speed  # noqa: E402
from app.vision.engine import engine  # noqa: E402

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "data/eval/v3.mp4"
AT_SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 6.67


def main() -> int:
    cap = cv2.VideoCapture(VIDEO)
    if not cap.isOpened():
        print("无法打开", VIDEO)
        return 1
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
    target = int(AT_SEC * fps)
    key = "diag"
    step = max(1, int(round(fps * 0.33)))

    frame_idx = 0
    last = None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step == 0:
            t = frame_idx / fps
            res = engine.infer(frame, None, t, True, key)
            if abs(t - AT_SEC) < 0.17:
                last = (t, res)
                break
        frame_idx += 1
    cap.release()
    engine.reset(key)

    if last is None:
        print("未定位到该时间点")
        return 1
    t, res = last
    tracks = [x for x in res.tracks if x.misses == 0 and x.confirmed]
    print(f"时间 {t:.2f}s  确认轨迹 {len(tracks)} 条  tid={[x.tid for x in tracks]}")
    print(f"阈值：距离比<={settings.GROUP_BULLY_DIST_RATIO}  "
          f"facing>={settings.ARGUE_FACING_MIN}  "
          f"动作者 wrist>={settings.FIGHT_WRIST_SPEED * 0.5:.2f}")
    print("-" * 78)
    adj: dict[int, set[int]] = {x.tid: set() for x in tracks}
    n = len(tracks)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = tracks[i], tracks[j]
            dist = float(np.linalg.norm(a.center - b.center))
            mean_h = (a.height + b.height) / 2
            ratio = dist / max(1e-6, mean_h)
            fs = facing_score(a, b)
            in_dist = ratio <= settings.GROUP_BULLY_DIST_RATIO
            conn = in_dist and fs >= settings.ARGUE_FACING_MIN
            if conn:
                adj[a.tid].add(b.tid)
                adj[b.tid].add(a.tid)
            mark = "连边" if conn else ("距离超限" if not in_dist else "朝向不足")
            print(f"  #{a.tid:>2}–#{b.tid:<2} 距离比={ratio:5.2f} facing={fs:5.2f}  {mark}"
                  f"  | 腕速 {wrist_speed(a):5.2f}/{wrist_speed(b):5.2f}")
    print("-" * 78)
    for tr in tracks:
        up = wrist_speed(tr)
        ra = arm_raised(tr.kpts)
        print(f"  #{tr.tid:>2} 腕速={up:5.2f}  抬手={str(ra):<5} "
              f"攻击姿态={str(ra or up >= settings.FIGHT_WRIST_SPEED * 0.5):<5}  邻居={sorted(adj[tr.tid])}")
    print("-" * 78)
    print("  攻击性近邻分析（距离近 + 有攻击姿态）：")
    for tr in tracks:
        atk = [x for x in tracks if x.tid != tr.tid
               and float(np.linalg.norm(x.center - tr.center)) / max(1e-6, (x.height + tr.height) / 2)
               <= settings.GROUP_BULLY_DIST_RATIO
               and (arm_raised(x.kpts) or wrist_speed(x) >= settings.FIGHT_WRIST_SPEED * 0.5)]
        print(f"    #{tr.tid} 的攻击性近邻 {[x.tid for x in atk]}"
              f"  自身有攻击姿态={arm_raised(tr.kpts) or wrist_speed(tr) >= settings.FIGHT_WRIST_SPEED * 0.5}")
    print("-" * 78)
    for tr in tracks:
        nbrs = adj[tr.tid]
        if len(nbrs) < settings.GROUP_BULLY_MIN_ATTACKERS:
            print(f"  #{tr.tid}：邻居 {len(nbrs)} < {settings.GROUP_BULLY_MIN_ATTACKERS}，不构成围困")
            continue
        possible = len(nbrs) * (len(nbrs) - 1) / 2
        edges = sum(1 for x in nbrs for y in nbrs if x < y and y in adj[x])
        coh = edges / possible if possible else 0.0
        moving = sum(1 for td in nbrs
                     if wrist_speed(next(x for x in tracks if x.tid == td)) >= settings.FIGHT_WRIST_SPEED * 0.5)
        print(f"  #{tr.tid}：邻居 {sorted(nbrs)}  内聚度={coh:.2f}"
              f"（上限 {settings.GROUP_BULLY_MAX_COHESION}）  动作者={moving}"
              f"（下限 {settings.GROUP_BULLY_MIN_MOTION}）"
              f"  → {'构成围困' if coh <= settings.GROUP_BULLY_MAX_COHESION and moving >= settings.GROUP_BULLY_MIN_MOTION else '不成立'}")
    print("\n本帧实际行为：", [(h.event_type, h.track_ids, round(h.confidence, 2)) for h in res.behaviors])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
