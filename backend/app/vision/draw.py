"""检测结果可视化：目标框、轨迹 ID、骨架、行为标签与画面 HUD。"""
from __future__ import annotations

import time

import cv2
import numpy as np

from app.vision.behaviors import BehaviorHit
from app.vision.types import SKELETON, Track

# BGR
COLORS: dict[str, tuple[int, int, int]] = {
    "person": (240, 214, 47),
    "fall": (32, 176, 255),
    "smoke": (65, 195, 215),
    "fight": (109, 77, 255),
    "argue": (61, 138, 255),
    "crowd": (255, 123, 155),
}
_WHITE = (240, 245, 250)
_DARK = (18, 14, 10)


def _color(event: str) -> tuple[int, int, int]:
    return COLORS.get(event, COLORS["person"])


def annotate(
    frame: np.ndarray,
    tracks: list[Track],
    behaviors: list[BehaviorHit] | None = None,
    show_skeleton: bool = True,
    show_hud: bool = True,
) -> np.ndarray:
    """在 BGR 帧上绘制感知结果，返回新帧（不修改入参）。"""
    img = frame.copy()
    h, w = img.shape[:2]
    behaviors = behaviors or []

    # 轨迹 -> 命中的行为（用于着色与打标）
    tag: dict[int, BehaviorHit] = {}
    for b in behaviors:
        for tid in b.track_ids:
            tag[tid] = b
        if b.bbox is not None and not b.track_ids:
            _draw_box(img, b.bbox, w, h, _color(b.event_type), f"{_label(b)} {b.confidence:.2f}")

    for tr in tracks:
        if tr.misses > 0:
            continue
        hit = tag.get(tr.tid)
        color = _color(hit.event_type) if hit else _color("person")
        if hit:
            _pulse_box(img, tr.bbox, w, h, color)
        else:
            _draw_box(img, tr.bbox, w, h, color, None)
        _draw_label(img, tr.bbox, w, h, f"#{tr.tid} " + (f"{_label(hit)} {hit.confidence:.2f}" if hit else "person"), color)
        if show_skeleton and tr.kpts is not None:
            _draw_skeleton(img, tr.kpts, w, h, color)

    if show_hud:
        _draw_hud(img, tracks, behaviors)
    return img


def _label(hit: BehaviorHit) -> str:
    from app.core.config import behavior_meta

    return str(behavior_meta(hit.event_type)["label"])


def _to_px(bbox: np.ndarray, w: int, h: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)


def _draw_box(img, bbox, w, h, color, label: str | None) -> None:
    x1, y1, x2, y2 = _to_px(bbox, w, h)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
    if label:
        _text(img, label, (x1, max(14, y1 - 6)), color)


def _pulse_box(img, bbox, w, h, color) -> None:
    """异常目标：加粗框 + 外发光角标。"""
    x1, y1, x2, y2 = _to_px(bbox, w, h)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 3, cv2.LINE_AA)
    for cx, cy, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(img, (cx, cy), (cx + dx * 14, cy), color, 4, cv2.LINE_AA)
        cv2.line(img, (cx, cy), (cx, cy + dy * 14), color, 4, cv2.LINE_AA)


def _draw_label(img, bbox, w, h, text: str, color) -> None:
    x1, y1, x2, y2 = _to_px(bbox, w, h)
    _text(img, text, (x1, max(14, y1 - 6)), color)


def _text(img, text: str, org: tuple[int, int], color) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.5
    thick = 1
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    x, y = org
    cv2.rectangle(img, (x, y - th - 5), (x + tw + 8, y + base - 1), color, -1, cv2.LINE_AA)
    cv2.putText(img, text, (x + 4, y - 2), font, scale, _DARK, thick, cv2.LINE_AA)


def _draw_skeleton(img, kpts: np.ndarray, w: int, h: int, color) -> None:
    pts = {}
    for i in range(len(kpts)):
        x, y, c = kpts[i][:3]
        if c >= 0.3 and (x > 0 or y > 0):
            pts[i] = (int(x * w), int(y * h))
    for a, b in SKELETON:
        if a in pts and b in pts:
            cv2.line(img, pts[a], pts[b], color, 2, cv2.LINE_AA)
    for p in pts.values():
        cv2.circle(img, p, 3, _WHITE, -1, cv2.LINE_AA)
        cv2.circle(img, p, 3, color, 1, cv2.LINE_AA)


def _draw_hud(img, tracks: list[Track], behaviors: list[BehaviorHit]) -> None:
    h, w = img.shape[:2]
    bar_h = 30
    overlay = img[0:bar_h, 0:w].copy()
    cv2.rectangle(overlay, (0, 0), (w, bar_h), (10, 14, 22), -1)
    cv2.addWeighted(overlay, 0.72, img[0:bar_h, 0:w], 0.28, 0, img[0:bar_h, 0:w])

    people = len([t for t in tracks if t.misses == 0])
    cv2.putText(img, f"CAMPUS SENTINEL  PEOPLE:{people}", (12, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 214, 47), 1, cv2.LINE_AA)

    if behaviors:
        names = " ".join(sorted({_label(b) for b in behaviors}))
        cv2.putText(img, names, (260, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (109, 77, 255), 1, cv2.LINE_AA)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    (tw, _), _ = cv2.getTextSize(stamp, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    cv2.putText(img, stamp, (max(12, w - tw - 14), 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (200, 210, 225), 1, cv2.LINE_AA)
