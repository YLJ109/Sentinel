"""运行时可变配置：检测能力开关 + 推理设备。

与 ``core.config.settings`` 的分工：
- ``settings``：启动时从环境变量读取、进程内不可变，用于部署期参数（阈值、路径、凭据）
- 本模块：值班人员在界面上随时可调的开关，落库持久化，重启后仍然生效

引擎的推理热路径（可能跑在线程里）通过 ``runtime_config.capability(...)`` 同步读取，
因此这里在内存里维护一份，写操作才落库，避免每帧都查数据库。
"""
from __future__ import annotations

import logging

from app.core.config import settings

log = logging.getLogger("core.runtime")

# 能力开关：key -> (默认值, 中文名)
CAPABILITY_KEYS: dict[str, tuple[bool, str]] = {
    "person": (True, "人体检测"),
    "pose": (True, "人体骨架"),
    "face": (True, "人脸检测"),
    "fall": (True, "跌倒检测"),
    "fight": (True, "打架检测"),
    "argue": (True, "吵架检测"),
    "smoke": (True, "抽烟检测"),
    "crowd": (True, "聚集检测"),
    "speech": (True, "语音识别"),
    "keyword": (True, "关键词检测"),
}

CAP_PREFIX = "cap:"
DEVICE_KEY = "device"

# 允许切换的推理设备（auto 表示有 CUDA 用 GPU，否则 CPU）
ALLOWED_DEVICES = ("auto", "cpu", "cuda:0", "cuda:1")


class RuntimeConfig:
    """内存缓存 + 落库持久化的运行时配置。"""

    def __init__(self) -> None:
        self._caps: dict[str, bool] = {k: default for k, (default, _) in CAPABILITY_KEYS.items()}
        self._device: str = ""  # 空 = 跟随 settings.DEVICE

    # ---------- 读取（引擎热路径，必须同步且廉价）----------
    def capability(self, key: str) -> bool:
        return self._caps.get(key, True)

    @property
    def capabilities(self) -> dict[str, bool]:
        return dict(self._caps)

    @property
    def device(self) -> str:
        """当前期望的推理设备标识（可能仍为 auto）。"""
        return self._device or settings.DEVICE

    @property
    def device_locked(self) -> bool:
        """是否被显式设置过（界面上切换过）。"""
        return bool(self._device)

    # ---------- 落库 ----------
    @staticmethod
    async def _read_rows() -> dict[str, str]:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models import AppSetting

        async with SessionLocal() as db:
            rows = list((await db.execute(select(AppSetting))).scalars().all())
        return {r.key: r.value for r in rows}

    @staticmethod
    async def _write(key: str, value: str) -> None:
        from app.core.db import SessionLocal
        from app.models import AppSetting

        async with SessionLocal() as db:
            row = await db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
            await db.commit()

    async def load(self) -> None:
        """启动时载入已保存的开关与设备。"""
        try:
            rows = await self._read_rows()
        except Exception as e:  # 数据库未就绪时用默认值继续，不阻断启动
            log.warning("运行时配置读取失败，使用默认值：%s", e)
            return

        for key in CAPABILITY_KEYS:
            raw = rows.get(CAP_PREFIX + key)
            if raw is not None:
                self._caps[key] = raw.strip() not in ("0", "false", "False", "")
        if rows.get(DEVICE_KEY):
            self._device = rows[DEVICE_KEY].strip()
        log.info("运行时配置已载入：设备=%s，关闭的能力=%s",
                 self.device, [k for k, v in self._caps.items() if not v] or "无")

    # ---------- 写入 ----------
    async def set_capability(self, key: str, enabled: bool) -> None:
        if key not in CAPABILITY_KEYS:
            raise KeyError(key)
        self._caps[key] = bool(enabled)
        await self._write(CAP_PREFIX + key, "1" if enabled else "0")

    async def set_device(self, device: str) -> None:
        self._device = "" if device == "auto" else device
        await self._write(DEVICE_KEY, self._device or "auto")


runtime_config = RuntimeConfig()
