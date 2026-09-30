"""运行时可变配置：检测能力开关 + 推理设备。

与 ``core.config.settings`` 的分工：
- ``settings``：启动时从环境变量读取、进程内不可变，用于部署期参数（阈值、路径、凭据）
- 本模块：值班人员在界面上随时可调的开关，落库持久化，重启后仍然生效

引擎的推理热路径（可能跑在线程里）通过 ``runtime_config.capability(...)`` 同步读取，
因此这里在内存里维护一份，写操作才落库，避免每帧都查数据库。
"""
from __future__ import annotations

import json
import logging

from app.core.config import settings

log = logging.getLogger("core.runtime")

# 能力开关：key -> (默认值, 中文名)
CAPABILITY_KEYS: dict[str, tuple[bool, str]] = {
    "person": (True, "人体检测"),
    "pose": (True, "人体骨架"),
    "face": (True, "人脸检测"),
    # 人脸识别需要额外的特征模型（SFace 权重），单独开关便于在算力紧张时只保留检测
    "face_id": (True, "人脸识别"),
    "emotion": (True, "情绪识别"),
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
PARAM_PREFIX = "param:"

# 允许切换的推理设备（auto 表示有 CUDA 用 GPU，否则 CPU）
ALLOWED_DEVICES = ("auto", "cpu", "cuda:0", "cuda:1")

# ---------------------------------------------------------------- 可调参数表
# 界面上可调整的运行参数：key -> 元信息。
#
# hot 的含义（这一列必须准确，否则用户改了没反应会以为系统坏了）：
#   hot=True   改完立即生效 —— 引擎每帧都读 settings 对象，赋值后下一帧即生效
#   hot=False  需要重载模型或重启 —— 例如输入尺寸是绑定在已加载模型上的
#
# 这些参数的默认值一律取自 ``settings``（即环境变量），界面上的修改只是
# **运行时覆盖**，落库持久化，但不改变 .env 文件本身 —— 后者仍是部署期的基线。
TUNABLE: dict[str, dict[str, object]] = {
    # ---------- 推理性能 ----------
    "DETECT_CONF": {"group": "infer", "label": "检测置信度", "type": "float",
                    "min": 0.1, "max": 0.9, "step": 0.05, "hot": True,
                    "hint": "调高可减少误报，调低可提高召回"},
    "INFER_FPS_LIMIT": {"group": "infer", "label": "推理帧率上限", "type": "int",
                        "min": 1, "max": 30, "hot": True,
                        "hint": "每路每秒最多推理帧数，应不低于前端送检帧率"},
    "MOTION_GATE_ENABLED": {"group": "infer", "label": "运动门控", "type": "bool", "hot": True,
                            "hint": "画面静止时跳过推理，显著降低空场景算力占用"},
    "MOTION_DIFF_THRESHOLD": {"group": "infer", "label": "门控灵敏度阈值", "type": "float",
                              "min": 0.001, "max": 0.1, "step": 0.001, "hot": True,
                              "hint": "画面变化低于该值即视为静止"},
    "MOTION_GATE_FORCE_INTERVAL_SEC": {"group": "infer", "label": "强制推理间隔(秒)", "type": "float",
                                       "min": 1, "max": 120, "hot": True,
                                       "hint": "画面持续静止时多久强制推理一次"},

    # ---------- 行为判定阈值 ----------
    "FALL_ASPECT_RATIO": {"group": "threshold", "label": "跌倒·宽高比", "type": "float",
                          "min": 0.8, "max": 2.5, "step": 0.05, "hot": True},
    "FALL_TORSO_ANGLE": {"group": "threshold", "label": "跌倒·躯干角(度)", "type": "float",
                         "min": 20, "max": 90, "step": 1, "hot": True},
    "FALL_MIN_FRAMES": {"group": "threshold", "label": "跌倒·确认帧数", "type": "int",
                        "min": 1, "max": 30, "hot": True},
    "FIGHT_DIST_RATIO": {"group": "threshold", "label": "打架·距离比", "type": "float",
                         "min": 0.5, "max": 4.0, "step": 0.05, "hot": True},
    "FIGHT_WRIST_SPEED": {"group": "threshold", "label": "打架·手腕速度", "type": "float",
                          "min": 0.2, "max": 2.0, "step": 0.02, "hot": True},
    "FIGHT_MIN_FRAMES": {"group": "threshold", "label": "打架·确认帧数", "type": "int",
                         "min": 1, "max": 30, "hot": True},
    "ARGUE_DIST_RATIO": {"group": "threshold", "label": "争吵·距离比", "type": "float",
                         "min": 0.5, "max": 4.0, "step": 0.05, "hot": True},
    "ARGUE_MIN_FRAMES": {"group": "threshold", "label": "争吵·确认帧数", "type": "int",
                         "min": 1, "max": 60, "hot": True},
    "ARGUE_FACING_MIN": {"group": "threshold", "label": "争吵·对峙姿态下限", "type": "float",
                         "min": 0.0, "max": 1.0, "step": 0.05, "hot": True},
    "SMOKE_HAND_HEAD_RATIO": {"group": "threshold", "label": "抽烟·手鼻距离比", "type": "float",
                              "min": 0.2, "max": 1.5, "step": 0.02, "hot": True},
    "SMOKE_MIN_FRAMES": {"group": "threshold", "label": "抽烟·确认帧数", "type": "int",
                         "min": 1, "max": 60, "hot": True},
    "CROWD_MIN_PEOPLE": {"group": "threshold", "label": "聚集·人数阈值", "type": "int",
                         "min": 2, "max": 30, "hot": True},
    "BULLY_SCORE_THRESHOLD": {"group": "threshold", "label": "欺凌·判定阈值", "type": "float",
                              "min": 0.1, "max": 1.0, "step": 0.02, "hot": True,
                              "hint": "超过该分数判为欺凌，否则判为对等冲突"},
    "GROUP_BULLY_MIN_ATTACKERS": {"group": "threshold", "label": "群体欺凌·同时卷入人数",
                                  "type": "int", "min": 1, "max": 10, "hot": True,
                                  "hint": "一人与至少几人同时发生肢体冲突才判为群体欺凌"},
    "GROUP_BULLY_MAX_COHESION": {"group": "threshold", "label": "群体欺凌·同伴内聚度上限",
                                 "type": "float", "min": 0.0, "max": 1.0, "step": 0.02, "hot": True,
                                 "hint": "卷入者彼此也互殴的比例；星型（多对一）时接近 0，"
                                         "混战互殴时偏高。调低更严格"},
    "GROUP_BULLY_EDGE_FRAMES": {"group": "threshold", "label": "群体欺凌·连边帧数", "type": "int",
                                "min": 1, "max": 20, "hot": True,
                                "hint": "一对人近窗内打斗达到几帧才算「互相冲突」"},
    "GROUP_BULLY_MIN_FRAMES": {"group": "threshold", "label": "群体欺凌·确认帧数", "type": "int",
                               "min": 1, "max": 30, "hot": True},
    "EVENT_VOTE_WINDOW": {"group": "threshold", "label": "事件投票窗口(帧)", "type": "int",
                          "min": 3, "max": 60, "hot": True},

    # ---------- 视频文件采样 ----------
    "VIDEO_MOTION_THRESHOLD": {"group": "video", "label": "运动量阈值", "type": "float",
                               "min": 0.001, "max": 0.1, "step": 0.001, "hot": True,
                               "hint": "帧间差分低于该值视为静止画面，转入稀疏采样"},
    "VIDEO_SAMPLE_ACTIVE_SEC": {"group": "video", "label": "动作时采样间隔(秒)", "type": "float",
                                "min": 0.02, "max": 1.0, "step": 0.01, "hot": True,
                                "hint": "与「打架确认帧数」联动：一段 0.3~0.4 秒的短促冲突"
                                        "至少要采到 4 帧（即 ≤0.09 秒）才不会被漏掉"},
    "VIDEO_SAMPLE_IDLE_SEC": {"group": "video", "label": "静止时采样间隔(秒)", "type": "float",
                              "min": 0.05, "max": 3.0, "step": 0.05, "hot": True,
                              "hint": "画面没动时的采样间隔，用于压缩总分析时长"},
    "VIDEO_ACTIVE_HOLD_SEC": {"group": "video", "label": "动作持续采样时长(秒)", "type": "float",
                              "min": 0.1, "max": 5.0, "step": 0.1, "hot": True,
                              "hint": "检测到动作后维持密集采样的时间，覆盖整段冲突"},

    # ---------- 跟踪与人数统计 ----------
    "TRACK_CONFIRM_HITS": {"group": "track", "label": "轨迹确认帧数", "type": "int",
                           "min": 1, "max": 10, "hot": True,
                           "hint": "连续命中该帧数后目标框才对外显示；调高可过滤单帧误检"},
    "TRACK_TTL_SEC": {"group": "track", "label": "轨迹存活时长(秒)", "type": "float",
                      "min": 0.2, "max": 5.0, "step": 0.1, "hot": True,
                      "hint": "丢失后多久回收轨迹；调高更抗遮挡，但会增加 ID 串号风险"},
    "TRACK_MAX_EXTRAP": {"group": "track", "label": "最大外推帧数", "type": "int",
                         "min": 1, "max": 60, "hot": True,
                         "hint": "遮挡后靠预测框延续的最大帧数，超过则冻结，防止框甩出画面"},
    "PEOPLE_WINDOW": {"group": "track", "label": "人数滤波窗口(帧)", "type": "int",
                      "min": 1, "max": 30, "hot": True,
                      "hint": "对最近若干帧人数取中位数，抑制人数跳变"},
    "PEOPLE_UP_HOLD": {"group": "track", "label": "人数上调确认(帧)", "type": "int",
                       "min": 1, "max": 30, "hot": True},
    "PEOPLE_DOWN_HOLD": {"group": "track", "label": "人数下调确认(帧)", "type": "int",
                         "min": 1, "max": 60, "hot": True,
                         "hint": "比上调更长，避免短暂遮挡就把人数打下去"},

    # ---------- 报警策略 ----------
    "ALARM_MIN_CONFIDENCE": {"group": "alarm", "label": "报警最低置信度", "type": "float",
                             "min": 0.3, "max": 0.95, "step": 0.05, "hot": True},
    "ALARM_COOLDOWN_SEC": {"group": "alarm", "label": "报警冷却(秒)", "type": "int",
                           "min": 0, "max": 120, "hot": True},
    "ALARM_MERGE_WINDOW_SEC": {"group": "alarm", "label": "事件合并窗口(秒)", "type": "int",
                               "min": 0, "max": 120, "hot": True},
    "FUSION_ENABLED": {"group": "alarm", "label": "多模态证据融合", "type": "bool", "hot": True,
                       "hint": "视觉与语音在时间窗内互相印证时提升报警可信度"},
    "FUSION_WINDOW_SEC": {"group": "alarm", "label": "融合时间窗(秒)", "type": "float",
                          "min": 1, "max": 60, "hot": True,
                          "hint": "需覆盖语音成句转写的延迟（通常 2~5 秒）"},
    "FUSION_BOOST": {"group": "alarm", "label": "融合置信度加权", "type": "float",
                     "min": 0.0, "max": 0.5, "step": 0.01, "hot": True},

    # ---------- 取证与留存 ----------
    "EVIDENCE_KEEP_DAYS": {"group": "evidence", "label": "取证留存天数", "type": "int",
                           "min": 1, "max": 3650, "hot": True},
    "EVIDENCE_JPEG_QUALITY": {"group": "evidence", "label": "截图 JPEG 质量", "type": "int",
                              "min": 40, "max": 100, "hot": True},
    "CLIP_PRE_SECONDS": {"group": "evidence", "label": "片段前录(秒)", "type": "int",
                         "min": 0, "max": 30, "hot": True},
    "CLIP_POST_SECONDS": {"group": "evidence", "label": "片段后延(秒)", "type": "int",
                          "min": 0, "max": 30, "hot": True},
    "PRIVACY_ZONES": {"group": "evidence", "label": "隐私遮蔽区域", "type": "json", "hot": True,
                      "hint": "JSON 数组，命中区域内的目标不产生事件、不落取证"},
}


def _coerce(key: str, value: object, spec: dict) -> object:
    """按参数元信息做类型转换与范围钳制。

    不做校验会有两类后果：字符串 "0.5" 传进数值比较处直接抛异常，
    或用户填了 999 把阈值撑到无意义的值。钳制到 [min, max] 比报错更友好 ——
    多数场景下用户的意图是"调到最灵敏/最保守"，而非故意越界。
    """
    kind = str(spec.get("type", "float"))
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    if kind == "json":
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
        json.loads(text)          # 校验：不合法直接抛 ValueError，由调用方转成 400
        return text

    try:
        num = int(value) if kind == "int" else float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{key} 需要{'整数' if kind == 'int' else '数值'}") from e

    lo, hi = spec.get("min"), spec.get("max")
    if isinstance(lo, (int, float)) and num < lo:
        num = lo
    if isinstance(hi, (int, float)) and num > hi:
        num = hi
    return num


class RuntimeConfig:
    """内存缓存 + 落库持久化的运行时配置。"""

    def __init__(self) -> None:
        self._caps: dict[str, bool] = {k: default for k, (default, _) in CAPABILITY_KEYS.items()}
        self._device: str = ""  # 空 = 跟随 settings.DEVICE
        self._params: dict[str, object] = {}   # 被界面覆盖过的参数
        # 参数被覆盖前的原始值（即 .env 基线）。"恢复默认"要还原到它，
        # 而不是还原到代码里的常量 —— 否则校方在 .env 里的定制会被重置抹掉。
        self._defaults: dict[str, object] = {}

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

        # 回放参数覆盖：直接写回 settings，使引擎读到"界面设置过的值"。
        # 单个参数值非法只跳过它并告警，不影响其余参数与启动流程。
        for raw_key, raw_val in rows.items():
            if not raw_key.startswith(PARAM_PREFIX):
                continue
            name = raw_key[len(PARAM_PREFIX):]
            spec = TUNABLE.get(name)
            if spec is None:
                continue
            try:
                value = _coerce(name, raw_val, spec)
                if name not in self._defaults:
                    self._defaults[name] = getattr(settings, name, None)   # .env 基线
                setattr(settings, name, value)
                self._params[name] = value
            except Exception as e:  # noqa: BLE001
                log.warning("参数覆盖值无效，已忽略：%s=%s（%s）", name, raw_val, e)

        log.info("运行时配置已载入：设备=%s，关闭的能力=%s，参数覆盖=%d 项",
                 self.device, [k for k, v in self._caps.items() if not v] or "无", len(self._params))

    # ---------- 可调参数 ----------
    @property
    def param_overrides(self) -> dict[str, object]:
        return dict(self._params)

    def param(self, key: str) -> object:
        """读取参数当前生效值（已被覆盖则返回覆盖值）。"""
        return getattr(settings, key, None)

    async def set_param(self, key: str, value: object) -> object:
        """设置可调参数并立即生效。

        实现上直接写入全局 ``settings`` 对象 —— 语义等价于"用运行时值覆盖环境变量"。
        这样引擎与业务代码无需任何改动（它们一直读 ``settings.XXX``），
        也彻底避免了"设置页改了、实际判定却没变"这类极难排查的问题。
        代价是 ``settings`` 不再是纯只读的部署期配置，因此这里只允许
        ``TUNABLE`` 白名单内的键，防止误改密钥、路径等敏感项。
        """
        spec = TUNABLE.get(key)
        if spec is None:
            raise KeyError(key)
        # 首次覆盖前记住基线值，供"恢复默认"使用
        if key not in self._defaults:
            self._defaults[key] = getattr(settings, key, None)
        coerced = _coerce(key, value, spec)
        setattr(settings, key, coerced)
        self._params[key] = coerced
        await self._write(PARAM_PREFIX + key,
                          coerced if isinstance(coerced, str) else json.dumps(coerced, ensure_ascii=False))

        # 隐私遮蔽区域被解析后缓存过，改完必须让它重新解析，否则新区域不生效
        if key == "PRIVACY_ZONES":
            from app.vision import privacy

            privacy.invalidate()
        return coerced

    async def clear_param(self, key: str) -> None:
        """清除运行时覆盖，恢复到 .env 基线值。

        注意恢复的是**启动时读到的环境变量值**，不是代码里的常量 ——
        否则校方在 .env 里做的定制会被一次"恢复默认"抹掉。
        """
        if key not in TUNABLE:
            raise KeyError(key)

        base = self._defaults.pop(key, None)
        if base is not None:
            setattr(settings, key, base)
        self._params.pop(key, None)

        from sqlalchemy import delete

        from app.core.db import SessionLocal
        from app.models import AppSetting

        async with SessionLocal() as db:
            await db.execute(delete(AppSetting).where(AppSetting.key == PARAM_PREFIX + key))
            await db.commit()

        if key == "PRIVACY_ZONES":
            from app.vision import privacy

            privacy.invalidate()

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
