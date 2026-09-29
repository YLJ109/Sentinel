"""请求/响应数据模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# 角色白名单：放在模型层约束，避免各处接口重复手写校验、也便于 OpenAPI 直接展示可选值
Role = Literal["admin", "operator", "viewer"]


# ---------- 鉴权 ----------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = ""
    role: Role = "operator"


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: Role | None = None
    disabled: bool | None = None


class PasswordChange(BaseModel):
    """自助改密需带原密码；管理员重置时仅用 new_password。"""

    old_password: str = ""
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str = ""
    role: str
    disabled: bool = False
    created_at: datetime | None = None


# ---------- 摄像头 ----------
class CameraCreate(BaseModel):
    name: str
    code: str | None = None
    location: str = ""
    source_type: str = "webcam"
    source_url: str = ""
    device_id: str | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    location: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    device_id: str | None = None
    enabled: bool | None = None


class CameraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: str | None = None
    location: str = ""
    source_type: str
    source_url: str = ""
    device_id: str | None = None
    enabled: bool = True
    created_at: datetime | None = None


class DeviceItem(BaseModel):
    """浏览器 enumerateDevices() 枚举到的一个本机视频设备。"""

    device_id: str
    label: str = ""
    index: int = 0


class CameraSyncRequest(BaseModel):
    """用本机真实设备列表对齐摄像头表（不伪造、不预置）。"""

    devices: list[DeviceItem] = []


# ---------- 系统运行时开关 ----------
class CapabilityToggle(BaseModel):
    """检测能力开关。"""

    enabled: bool


class DeviceSwitch(BaseModel):
    """推理设备热切换：auto / cpu / cuda:0 …。"""

    device: str = "auto"


# ---------- 检测事件 ----------
class DetectionEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    camera_id: int | None
    event_type: str
    label: str
    confidence: float
    frame_time: float
    thumbnail_path: str | None
    bbox: str | None
    track_ids: str | None = None
    detail: str | None = None
    is_bullying: bool
    created_at: datetime | None


# ---------- 报警 ----------
class AlarmCreate(BaseModel):
    event_id: int | None = None
    level: str = "high"
    reason: str
    source: str = "manual"
    note: str | None = None


class AlarmUpdate(BaseModel):
    status: str | None = None
    note: str | None = None
    feedback_label: str | None = None   # false_positive / not_bullying / confirmed / other
    ignored_reason: str | None = None


class AlarmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_id: int | None
    camera_id: int | None = None
    level: str
    reason: str
    source: str
    status: str
    evidence_paths: str | None
    created_at: datetime | None
    resolved_at: datetime | None
    creator_id: int | None
    handler_id: int | None = None
    feedback_label: str | None = None
    ignored_reason: str | None = None
    note: str | None


# ---------- 聊天日志 ----------
class ChatLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    camera_id: int | None
    alarm_id: int | None
    speaker: str
    text: str
    hit_keywords: str | None
    confidence: float
    start_time: float
    end_time: float
    is_final: bool = True
    created_at: datetime | None


# ---------- 视频记录 ----------
class VideoRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    camera_id: int | None
    filename: str
    original_path: str | None = None    # 原始上传文件路径（前端据此回放原片）
    processed_path: str | None
    duration: float
    status: str
    progress: float
    event_count: int
    alarm_count: int
    created_at: datetime | None


# ---------- 检测回调（实时帧）----------
class FrameDetectRequest(BaseModel):
    camera_id: int | None = None
    image_b64: str                               # base64 jpeg
    timestamp: float = 0.0


class DetectionBox(BaseModel):
    """一个被跟踪的人体目标。"""
    track_id: int | None = None
    event_type: str = "person"
    label: str = "人员"
    confidence: float = 0.0
    bbox: list[float] | None = None             # [x1,y1,x2,y2] 归一化 0-1
    is_bullying: bool = False
    kpts: list[list[float]] | None = None       # 17 × [x, y, conf]，归一化


class BehaviorBox(BaseModel):
    """一次行为命中（已通过多帧投票）。"""
    event_type: str
    label: str
    confidence: float
    track_ids: list[int] = []
    bbox: list[float] | None = None
    is_bullying: bool = False
    detail: dict[str, Any] = {}


class FrameDetectResponse(BaseModel):
    boxes: list[DetectionBox] = []
    behaviors: list[BehaviorBox] = []
    faces: list[dict[str, Any]] = []
    people: int = 0
    infer_ms: float = 0.0
    alarm: dict[str, Any] | None = None         # 若触发报警则返回
    server_time: float


# ---------- 系统 / 引擎状态 ----------
class SystemModelsOut(BaseModel):
    device: str
    half: bool
    imgsz: int
    device_info: dict[str, Any] = {}
    models: dict[str, Any] = {}
    speech: dict[str, Any] = {}
    alarm_runtime: dict[str, Any] = {}
    pipelines: dict[str, Any] = {}


# ---------- 分页 ----------
class Page(BaseModel):
    """统一分页返回结构。"""

    total: int = 0
    offset: int = 0
    limit: int = 50


class IdsIn(BaseModel):
    """批量操作入参（如批量删除）。

    ``max_length`` 是必需的：ids 会直接展开成 ``IN (...)`` 语句，
    不设上限时一个请求就能拼出超长 SQL（SQLite 变量数上限约 999，超了直接报错，
    即使不报错也会让数据库做一次全表级扫描）。
    """

    ids: list[int] = Field(default_factory=list, max_length=500)


class AlarmPage(Page):
    items: list[AlarmOut] = []


class EventPage(Page):
    items: list[DetectionEventOut] = []


class ChatLogPage(Page):
    items: list[ChatLogOut] = []


class VideoPage(Page):
    items: list[VideoRecordOut] = []


# ---------- 运行日志 ----------
class LogEntry(BaseModel):
    id: int
    ts: float
    time: str
    level: str
    logger: str
    message: str
    traceback: str | None = None


class LogQueryOut(BaseModel):
    items: list[LogEntry] = []
    total: int = 0
    latest_id: int = 0
    error_count: int = 0
    warning_count: int = 0


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    camera_online: int
    camera_total: int
    today_events: int
    today_alarms: int
    pending_alarms: int
    event_type_breakdown: dict[str, int]
    recent_alarms: list[AlarmOut]
