"""隐私遮蔽：命中遮蔽区域的目标不产生事件，也不落任何取证。

为什么需要它：
    校园监控要覆盖楼道、天台、储物间等隐蔽场所（教育部专项行动明确要求
    「在楼道、天台、储物间等隐蔽场所做到视频监控全覆盖」），但这些位置
    往往紧邻卫生间、更衣区等敏感区域。把它们整块纳入监测既无必要，
    也容易引发师生抵触。隐私遮蔽让这些区域只保留「有人在」这一级信息，
    不产生行为事件、不落任何图像证据 —— 这是数据最小化原则在系统内的直接体现。

关于「浏览器端提取骨架、画面不出本地」这条路线：
    它需要在前端引入 ONNX Runtime（约 3MB wasm）与约 12MB 的姿态模型，
    会显著增加前端体积与客户端 CPU 占用；而校园摄像头画面本就要接入学校
    既有的视频监控平台，「画面不出浏览器」的实际隐私增益有限。
    因此这里选择**服务端数据最小化**：遮蔽区不落盘 + 取证留存期限
    （``EVIDENCE_KEEP_DAYS``）+ 敏感区域不做行为判定。
    两条路线并不互斥 —— 若后续确有「端侧离线」需求，再按 ONNX 路线叠加即可。
"""
from __future__ import annotations

import json
import logging

from app.core.config import settings

log = logging.getLogger("vision.privacy")

# 解析后的缓存：[(camera_id 或 None 表示全部, (x1, y1, x2, y2)), ...]
_zones: list[tuple[int | None, tuple[float, float, float, float]]] | None = None


def _load() -> list[tuple[int | None, tuple[float, float, float, float]]]:
    global _zones
    if _zones is not None:
        return _zones

    out: list[tuple[int | None, tuple[float, float, float, float]]] = []
    raw = (settings.PRIVACY_ZONES or "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except ValueError:
            log.warning("CAB_PRIVACY_ZONES 不是合法 JSON，已忽略：%s", raw[:80])
            items = []
        for it in items if isinstance(items, list) else []:
            try:
                rect = tuple(float(v) for v in it["rect"])
                if len(rect) != 4:
                    continue
                cam = it.get("camera")
                out.append((None if cam is None else int(cam), rect))  # type: ignore[arg-type]
            except (KeyError, TypeError, ValueError):
                log.warning("忽略格式错误的隐私区域项：%s", it)

    _zones = out
    return out


def invalidate() -> None:
    """参数变更后调用：清掉解析缓存，下次判定时重新读取配置。

    遮蔽区只在首次使用时解析一次（判定跑在每帧的热路径上，不能反复 json.loads），
    因此设置页改了区域之后必须显式失效，否则新区域不会生效。
    """
    global _zones
    _zones = None


def is_masked(bbox, camera_id: int | None) -> bool:
    """目标框中心落在任一遮蔽区内即视为被遮蔽。

    用中心点而不是框重叠面积：后者会让「一半身子在遮蔽区、一半在外」的目标
    来回抖动，导致事件时有时无。
    """
    zones = _load()
    if not zones or bbox is None:
        return False

    cx = (float(bbox[0]) + float(bbox[2])) / 2.0
    cy = (float(bbox[1]) + float(bbox[3])) / 2.0
    for cam, (x1, y1, x2, y2) in zones:
        if cam is not None and cam != camera_id:
            continue
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return True
    return False


def zones() -> list[dict]:
    """对外透出的遮蔽区清单（供前端可视化与运维核对）。"""
    return [{"camera": cam, "rect": list(rect)} for cam, rect in _load()]
