"""系统设置：可调参数的读取与修改。

四条设计原则：

1. **白名单制**。只允许修改 ``TUNABLE`` 中登记的运行期参数，
   绝不允许通过接口改密钥、路径、端口等部署期配置 —— 这些一旦能被远程修改，
   系统安全性就失去了基线。
2. **立即生效**。写入直接作用于全局 ``settings`` 对象，引擎下一帧即读到新值，
   不需要重启，也不会出现"设置页改了、判定逻辑却没变"的割裂。
3. **落库持久化**。改动写入 ``app_settings`` 表，重启后自动回放；
   ``.env`` 始终是部署期的基线，不会被界面修改污染。
4. **变更留痕**。每次修改记录「旧值 → 新值」，日志面板可见，便于回溯
   "某天误报突然变多是不是有人调过阈值"。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.core.runtime_config import TUNABLE, runtime_config
from app.models import User
from app.services import alarm as alarm_svc

router = APIRouter(prefix="/api/settings", tags=["系统设置"])
log = logging.getLogger("routers.settings")

_write = require_role("admin", "operator")

GROUP_LABELS: dict[str, str] = {
    "infer": "推理与性能",
    "video": "视频文件采样",
    "track": "跟踪与人数统计",
    "threshold": "行为判定阈值",
    "alarm": "报警策略",
    "evidence": "取证与留存",
}

# 仅管理员可改：涉及取证留存期限与隐私边界，属于合规相关配置
ADMIN_ONLY: set[str] = {
    "PRIVACY_ZONES", "EVIDENCE_KEEP_DAYS", "EVIDENCE_JPEG_QUALITY",
    "CLIP_PRE_SECONDS", "CLIP_POST_SECONDS",
}

# 只读展示项：这些参数绑定在**已加载的模型**或进程上，改了必须重载/重启，
# 因此不开放编辑，只在页面上显示当前值与改法，避免用户"改了没反应"。
READONLY_INFO: list[dict[str, str]] = [
    {"key": "IMGSZ", "label": "模型输入尺寸", "hint": "绑定在已加载模型上，改后需重载模型（在 .env 中设置 CAB_IMGSZ 并重启）"},
    {"key": "HALF", "label": "FP16 半精度", "hint": "随设备一起决定，切换推理设备时自动调整"},
    {"key": "DEVICE", "label": "推理设备默认值", "hint": "运行期请用「推理设备」切换，此值为 .env 基线"},
    {"key": "ENV", "label": "运行环境", "hint": "production 会启用更严格的安全校验"},
]


class ParamPatch(BaseModel):
    values: dict[str, Any] = Field(..., description="{参数名: 新值}")


def _serialize(key: str, spec: dict) -> dict:
    return {
        "key": key,
        "label": spec.get("label", key),
        "type": spec.get("type", "float"),
        "min": spec.get("min"),
        "max": spec.get("max"),
        "step": spec.get("step"),
        "hot": bool(spec.get("hot", True)),
        "hint": spec.get("hint"),
        "value": getattr(settings, key, None),
        "overridden": key in runtime_config.param_overrides,
        "admin_only": key in ADMIN_ONLY,
    }


@router.get("/schema")
async def settings_schema(_: User = Depends(get_current_user)) -> dict:
    """返回全部可调参数的元信息与当前值，供设置页渲染。

    分组与排序由后端的 ``TUNABLE`` 表决定 —— 前端不硬编码参数清单，
    这样新增一个可调参数只需要改后端一处。
    """
    groups: dict[str, list[dict]] = {}
    for key, spec in TUNABLE.items():
        groups.setdefault(str(spec.get("group", "other")), []).append(_serialize(key, spec))

    return {
        "groups": [
            {"key": g, "label": GROUP_LABELS.get(g, g), "items": items}
            for g, items in groups.items()
        ],
        "readonly": [
            {**item, "value": getattr(settings, item["key"], None)} for item in READONLY_INFO
        ],
        "overridden_count": len(runtime_config.param_overrides),
    }


@router.patch("")
async def update_settings(body: ParamPatch, user: User = Depends(_write)) -> dict:
    """批量更新参数。逐项独立处理，单项失败不影响其余项。"""
    if not body.values:
        return {"updated": 0, "failed": []}

    updated: dict[str, Any] = {}
    failed: list[dict[str, str]] = []

    for key, value in body.values.items():
        spec = TUNABLE.get(key)
        if spec is None:
            failed.append({"key": key, "reason": "不在可调参数白名单内"})
            continue
        if key in ADMIN_ONLY and user.role != "admin":
            failed.append({"key": key, "reason": "该参数仅管理员可修改"})
            continue

        old = getattr(settings, key, None)
        try:
            new = await runtime_config.set_param(key, value)
        except ValueError as e:
            failed.append({"key": key, "reason": str(e)})
            continue
        except Exception as e:  # noqa: BLE001
            failed.append({"key": key, "reason": f"写入失败：{e}"})
            continue

        if str(old) != str(new):
            # 变更留痕：便于回溯"某天误报变多是不是有人调过阈值"
            log.info("参数变更：%s %s → %s（by %s）", key, old, new, user.username)
        updated[key] = new

    return {"updated": len(updated), "values": updated, "failed": failed}


@router.post("/reset")
async def reset_settings(names: list[str] | None = None,
                         _: User = Depends(require_role("admin"))) -> dict:
    """把参数恢复为环境变量基线（删除运行时覆盖）。

    只清覆盖值，不动 .env —— 所谓"默认值"始终是部署时设定的基线，
    而不是代码里写死的常量，这样校方在 .env 里做的定制不会被界面重置抹掉。
    """
    targets = list(names) if names else list(runtime_config.param_overrides.keys())
    removed = 0
    for key in targets:
        if key not in TUNABLE:
            continue
        await runtime_config.clear_param(key)
        removed += 1
    return {"reset": removed, "targets": targets}


@router.get("/runtime")
async def runtime_snapshot(db: AsyncSession = Depends(get_db),
                           _: User = Depends(get_current_user)) -> dict:
    """运行态快照：报警冷却/去重状态，供设置页做"参数是否生效"的旁证。"""
    from app.vision.engine import engine

    return {
        "alarm": await alarm_svc.stats(db),
        "pipelines": engine.pipelines_status(),
        "overrides": runtime_config.param_overrides,
    }
