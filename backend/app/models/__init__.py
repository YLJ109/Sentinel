"""ORM 模型：用户 / 摄像头 / 检测事件 / 报警 / 聊天日志 / 视频记录 / 取证文件。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(32), default="operator")  # admin / operator / viewer
    hashed_password: Mapped[str] = mapped_column(String(255))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    events: Mapped[list["DetectionEvent"]] = relationship(back_populates="operator")
    alarms: Mapped[list["AlarmRecord"]] = relationship(
        back_populates="creator", foreign_keys="AlarmRecord.creator_id"
    )


class Camera(Base):
    """监控摄像头 / 视频源。"""
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    # 用户可编辑的摄像头编号；为空时前端回落显示 #id
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str] = mapped_column(String(255), default="")        # 安装位置（教学楼/操场…）
    source_type: Mapped[str] = mapped_column(String(32), default="webcam")  # webcam / rtsp / file
    source_url: Mapped[str] = mapped_column(String(512), default="")
    # 本机摄像头的浏览器设备 ID：用于「枚举真实设备 → 对齐摄像头表」时去重
    device_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    events: Mapped[list["DetectionEvent"]] = relationship(back_populates="camera")


class DetectionEvent(Base):
    """一次检测事件（经多帧投票确认的行为）。"""
    __tablename__ = "detection_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)  # fall / smoke / fight / argue / crowd / person / normal
    label: Mapped[str] = mapped_column(String(128))                  # 中文展示标签
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    frame_time: Mapped[float] = mapped_column(Float, default=0.0)    # 视频中的时间戳(秒)，0 表示实时流
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bbox: Mapped[str | None] = mapped_column(String(255), nullable=True)  # x1,y1,x2,y2（归一化）
    track_ids: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 关联轨迹 ID，逗号分隔
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)   # 判定特征 JSON（可解释性/再训练用）
    is_bullying: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    camera: Mapped["Camera | None"] = relationship(back_populates="events")
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    operator: Mapped["User | None"] = relationship(back_populates="events")
    alarms: Mapped[list["AlarmRecord"]] = relationship(back_populates="event")


class AlarmRecord(Base):
    """报警 / 预警记录。"""
    __tablename__ = "alarm_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("detection_events.id"), nullable=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(24), default="high")  # high / medium / low
    reason: Mapped[str] = mapped_column(String(512))                # 触发原因（行为+置信度/关键词）
    source: Mapped[str] = mapped_column(String(24), default="video")  # video / audio / manual
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending / handling / resolved / ignored
    evidence_paths: Mapped[str | None] = mapped_column(Text, nullable=True)  # 取证文件 JSON 列表
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    handler_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # 实际处置人
    creator: Mapped["User | None"] = relationship(back_populates="alarms", foreign_keys=[creator_id])
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 处置反馈：用于误报统计与再训练样本导出
    feedback_label: Mapped[str | None] = mapped_column(String(32), nullable=True)   # false_positive / not_bullying / confirmed / other
    ignored_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    event: Mapped["DetectionEvent | None"] = relationship(back_populates="alarms")


class KeywordRule(Base):
    """关键词规则（三档响应），支持增删改查与命中统计。

    为什么做成表而不是配置文件：
        学校想加一个方言词、或误报严重的词需要停用，都要改代码重启才生效；
        而且词表命中次数无从统计，没法回答"哪些词有用、哪些词一直在制造误报"。
    """
    __tablename__ = "keyword_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    word: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(24), default="custom")
    # alarm 触发报警 / warn 警告提示 / highlight 仅高亮
    level: Mapped[str] = mapped_column(String(16), default="warn", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 命中次数：用于反向优化词表（哪些词有用、哪些词一直在制造误报）
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    # 内置词允许停用但不允许删除，避免误删后无法恢复
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatLog(Base):
    """语音转写 + 关键词命中日志（取证聊天内容）。"""
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"), nullable=True)
    alarm_id: Mapped[int | None] = mapped_column(ForeignKey("alarm_records.id"), nullable=True)
    speaker: Mapped[str] = mapped_column(String(64), default="unknown")
    text: Mapped[str] = mapped_column(Text)
    hit_keywords: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 命中的关键词（顿号分隔）
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    start_time: Mapped[float] = mapped_column(Float, default=0.0)   # 音频相对起点(秒)
    end_time: Mapped[float] = mapped_column(Float, default=0.0)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)   # False 表示流式中间结果
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoRecord(Base):
    """上传 / 录制的视频检测任务记录。"""
    __tablename__ = "video_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    original_path: Mapped[str] = mapped_column(String(512))
    processed_path: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 标注后视频
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="processing")  # processing / done / failed
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    alarm_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceFile(Base):
    """取证文件（截图 / 剪辑片段 / 音频 / 转写文本）。"""
    __tablename__ = "evidence_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alarm_id: Mapped[int | None] = mapped_column(ForeignKey("alarm_records.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("detection_events.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))   # frame / clip / audio / transcript
    path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(64), default="application/octet-stream")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RuntimeState(Base):
    """带过期时间的运行时状态（报警冷却、事件去重），落库以便多 worker 一致。"""
    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[float] = mapped_column(Float, default=0.0, index=True)


class AppSetting(Base):
    """运行时可变的键值配置（检测能力开关、推理设备），落库以便重启后保持。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
