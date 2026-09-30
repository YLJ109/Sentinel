"""欺凌 / 打架 / 嬉闹 三分类判别 —— 合成场景可复现验证。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe scripts\\check_bullying.py

用**合成骨架序列**构造五类典型互动场景，逐帧送入 BehaviorAnalyzer，
打印互动对称性指标与最终判定。用途：
  ① 回归验证：调整阈值后确认判别逻辑没有被改坏；
  ② 竞赛材料中的「可复现实验」证据。

⚠️ 边界说明：合成数据只能验证**判别逻辑**本身是否正确，**不能**替代真实场景的
误报率 / 漏报率统计。后者必须在校园实拍片段（如 NWPU Campus 或自采数据）上完成。
论文与答辩中引用指标时务必标明数据来源、是否自划分，不可照抄他人论文数字。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Callable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vision.behaviors import BehaviorAnalyzer  # noqa: E402
from app.vision.types import (  # noqa: E402
    KP_L_ANKLE, KP_L_ELBOW, KP_L_HIP, KP_L_KNEE, KP_L_SHOULDER, KP_L_WRIST,
    KP_NOSE, KP_R_ANKLE, KP_R_ELBOW, KP_R_HIP, KP_R_KNEE, KP_R_SHOULDER, KP_R_WRIST,
    Track,
)

FPS = 10                 # 与前端送检帧率一致
DT = 1.0 / FPS
FRAMES = 18              # 约 1.8 秒


def person(cx: float, cy: float = 0.5, h: float = 0.5, w: float = 0.5,
           swing: float = 0.0, nose_drop: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """构造一个站立人形的 (bbox, kpts)。

    swing     —— 手腕**水平**摆动量，模拟挥臂/推搡；0 表示手臂静止。
                 刻意用水平方向而非竖直：竖直摆手会让手腕靠近鼻子，
                 误触发吸烟判定，干扰本脚本要验证的目标。
    nose_drop —— 头部下压量，模拟低头退让 / 被压制姿态
    """
    bbox = np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], dtype=float)
    sw = 0.30 * w
    k = np.zeros((17, 3), dtype=float)
    k[:, 2] = 0.9
    k[KP_NOSE] = [cx, cy - 0.35 * h + nose_drop, 0.9]
    k[KP_L_SHOULDER] = [cx - sw / 2, cy - 0.25 * h, 0.9]
    k[KP_R_SHOULDER] = [cx + sw / 2, cy - 0.25 * h, 0.9]
    k[KP_L_ELBOW] = [cx - sw / 2, cy - 0.05 * h, 0.9]
    k[KP_R_ELBOW] = [cx + sw / 2, cy - 0.05 * h, 0.9]
    # 手腕自然下垂在腹部高度，沿水平方向摆动（swing > 0 为双手张开）
    k[KP_L_WRIST] = [cx - sw / 2 - swing, cy + 0.10 * h, 0.9]
    k[KP_R_WRIST] = [cx + sw / 2 + swing, cy + 0.10 * h, 0.9]
    k[KP_L_HIP] = [cx - sw / 3, cy + 0.10 * h, 0.9]
    k[KP_R_HIP] = [cx + sw / 3, cy + 0.10 * h, 0.9]
    k[KP_L_KNEE] = [cx - sw / 3, cy + 0.30 * h, 0.9]
    k[KP_R_KNEE] = [cx + sw / 3, cy + 0.30 * h, 0.9]
    k[KP_L_ANKLE] = [cx - sw / 3, cy + 0.48 * h, 0.9]
    k[KP_R_ANKLE] = [cx + sw / 3, cy + 0.48 * h, 0.9]
    return bbox, k


def run_scenario(pos_a: Callable[[int], float], pos_b: Callable[[int], float],
                 swing_a: Callable[[int], float], swing_b: Callable[[int], float],
                 nose_drop_a: float = 0.0, nose_drop_b: float = 0.0) -> list:
    """逐帧推进一个场景，返回最后一帧的行为判定列表。"""
    analyzer = BehaviorAnalyzer()
    ta = Track(tid=1, bbox=np.zeros(4), kpts=None, conf=0.9)
    tb = Track(tid=2, bbox=np.zeros(4), kpts=None, conf=0.9)
    hits: list = []
    for i in range(FRAMES):
        t = i * DT
        ba, ka = person(pos_a(i), swing=swing_a(i), nose_drop=nose_drop_a)
        bb, kb = person(pos_b(i), swing=swing_b(i), nose_drop=nose_drop_b)
        for tr, bx, kp in ((ta, ba, ka), (tb, bb, kb)):
            tr.bbox = bx
            tr.kpts = kp
            tr.updated_at = t
            tr.push(t, bx, kp)
        hits = analyzer.analyze([ta, tb])
    return hits


def swing_amp(amp: float, period: int = 8) -> Callable[[int], float]:
    """周期摆动：用于产生可控的手腕速度。"""
    return lambda i: amp * math.sin(2 * math.pi * i / period)


def report(title: str, expect: str, hits: list) -> None:
    print(f"\n{title}")
    print(f"   期望：{expect}")
    if not hits:
        print("   结果：未报警")
        return
    for h in hits:
        label = {"bullying": "欺凌", "fight": "打架", "argue": "争吵",
                 "fall": "跌倒", "smoke": "疑似吸烟", "crowd": "人员聚集"}.get(h.event_type, h.event_type)
        d = h.detail
        keys = ("asymmetry", "retreat", "chase", "suppression", "bully_score")
        if all(k in d for k in keys):
            print(f"   结果：{label}（{h.event_type}） 置信度 {h.confidence}")
            print("         互动对称性指标："
                  + "  ".join(f"{k}={d[k]}" for k in keys))
        else:
            print(f"   结果：{label}（{h.event_type}） 置信度 {h.confidence}  特征={d}")


def main() -> None:
    print("=" * 76)
    print("欺凌 / 打架 / 嬉闹 三分类判别 · 合成场景验证")
    print(f"送检 {FPS} fps，共 {FRAMES} 帧（约 {FRAMES / FPS:.1f} 秒）")
    print("=" * 76)

    report(
        "① 单向欺凌：A 逼近并挥臂，B 后撤并低头",
        "应判为 bullying（欺凌）",
        run_scenario(
            pos_a=lambda i: 0.35 + 0.018 * i,
            pos_b=lambda i: 0.62 + 0.012 * i,
            swing_a=swing_amp(0.15),
            swing_b=lambda i: 0.0,
            nose_drop_b=0.10,
        ),
    )

    report(
        "② 对等互殴：双方相向逼近，且都猛烈挥臂",
        "应判为 fight（打架），不得判成欺凌或嬉闹",
        run_scenario(
            pos_a=lambda i: 0.35 + 0.007 * i,
            pos_b=lambda i: 0.65 - 0.007 * i,
            swing_a=swing_amp(0.15),
            swing_b=swing_amp(0.15),
        ),
    )

    report(
        "③ 课间嬉闹：贴近、轻微推搡、幅度对等",
        "应【不报警】（这是最主要的误报来源，必须被抑制）",
        run_scenario(
            pos_a=lambda i: 0.44 + 0.002 * math.sin(2 * math.pi * i / 8),
            pos_b=lambda i: 0.56 + 0.002 * math.sin(2 * math.pi * i / 8),
            swing_a=swing_amp(0.07),
            swing_b=swing_amp(0.07),
        ),
    )

    report(
        "④ 并肩同行：同速同向移动，无肢体动作",
        "应【不报警】",
        run_scenario(
            pos_a=lambda i: 0.30 + 0.015 * i,
            pos_b=lambda i: 0.55 + 0.015 * i,
            swing_a=lambda i: 0.0,
            swing_b=lambda i: 0.0,
        ),
    )

    report(
        "⑤ 排队贴近：位置固定、无动作",
        "应【不报警】",
        run_scenario(
            pos_a=lambda i: 0.45,
            pos_b=lambda i: 0.53,
            swing_a=lambda i: 0.0,
            swing_b=lambda i: 0.0,
        ),
    )

    print("\n" + "=" * 76)
    print("说明：以上为合成场景，仅验证判别逻辑。真实误报率/漏报率请在校园实拍")
    print("      片段上统计（参见 README「已知限制」与竞赛材料的实验设计章节）。")
    print("=" * 76)


if __name__ == "__main__":
    main()
