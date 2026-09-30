"""ORM 模型：用户 / 摄像头 / 检测事件 / 报警 / 聊天日志 / 视频记录 / 取证文件，
以及人员档案（学生/教师/管理人员）与人脸识别、合规留痕相关的表。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary,
                        String, Text, UniqueConstraint, func)
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
    # 离线视频分析产生的事件会带上视频任务 id，便于按"某一次检测"回放完整时间轴；
    # 实时链路该列为空。历史取证页据此区分两类来源。
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_records.id", ondelete="CASCADE"), nullable=True, index=True)
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
    # 离线视频的音频转写带视频任务 id，start_time/end_time 即为**视频时间轴**上的秒数，
    # 前端据此实现"点转写条目跳到视频对应位置"。
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_records.id", ondelete="CASCADE"), nullable=True, index=True)
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


# ============================================================================
# 人员档案（学生 / 教师 / 管理人员）
#
# 为什么分成三张表而不是一张 persons 表：
#   三类人员的业务字段差异大（学生有学号/班级/监护人，教师有职称/任教科目，
#   管理人员有职务/所属部门），分表后字段语义与唯一约束各自独立，不会出现
#   "一张表里一半字段对某类人永远是 NULL"的情况。
#
# 但人脸识别、同意留痕、事件关联这三块逻辑对三类人**完全一致**，因此把它们
# 抽到 FaceEnrollment / ConsentRecord 里用 (owner_type, owner_id) 多态关联，
# 避免同一套逻辑写三遍。多态外键没有数据库级约束，所以
# app/services/person_store.py 是唯一的写入口，删除人员时在同一事务内级联清理。
# ============================================================================


class SchoolClass(Base):
    """行政班级。"""
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # 如「初二(3)班」
    grade: Mapped[str] = mapped_column(String(32), default="", index=True)   # 年级，如「初二」
    # 班主任（关联教师表，教师被删时置空而不是级联删除）
    head_teacher_id: Mapped[int | None] = mapped_column(
        ForeignKey("teachers.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Student(Base):
    """学生档案。人脸特征与同意状态不在这里，见 FaceEnrollment。"""
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    gender: Mapped[str] = mapped_column(String(8), default="")               # 男 / 女 / 未填
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("classes.id", ondelete="SET NULL"), nullable=True, index=True)
    enroll_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 监护人信息：不满 14 周岁的学生人脸信息需取得监护人同意（见合规设计）
    guardian_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guardian_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    guardian_relation: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 父/母/其他
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # active 在读 / transferred 转学 / graduated 毕业 / suspended 休学
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    class_: Mapped["SchoolClass | None"] = relationship("SchoolClass", lazy="selectin")


class Teacher(Base):
    """教师档案。"""
    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    gender: Mapped[str] = mapped_column(String(8), default="")
    department: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # 所属科室/年级组
    title: Mapped[str | None] = mapped_column(String(32), nullable=True)                    # 职称，如「一级教师」
    subject: Mapped[str | None] = mapped_column(String(32), nullable=True)                  # 任教科目
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Staff(Base):
    """管理人员（行政/后勤/安保等非教学岗）。"""
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staff_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    gender: Mapped[str] = mapped_column(String(8), default="")
    department: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    position: Mapped[str | None] = mapped_column(String(32), nullable=True)   # 职务，如「德育处主任」
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 是否可登录本系统（与 users 表的关系仅在业务层体现，此处只做标记）
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FaceEnrollment(Base):
    """人脸建档主体（多态）：一条记录 = 一个人的"人脸档案"。

    把"同意状态"和"是否已建档"放在同一张表，是为了让**未授权**这件事在数据层
    就不可绕过：检索时先按 consent_status 过滤，未授权者连特征都不会进入内存索引。

    owner_type 取值：student / teacher / staff
    """
    __tablename__ = "face_enrollments"
    __table_args__ = (UniqueConstraint("owner_type", "owner_id", name="uq_enroll_owner"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(16), index=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)

    # none 未告知 / granted 已授权 / revoked 已撤回
    consent_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    # self 本人 / guardian 监护人（不满 14 周岁必须由监护人同意）
    consent_subject: Mapped[str | None] = mapped_column(String(16), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_evidence_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 注册原图与裁切头像。按《人脸识别技术应用安全管理办法》第八条，
    # 人脸信息只存于本机设备，不通过互联网对外传输。
    photo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)

    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FaceTemplate(Base):
    """人脸特征模板：一个人可有多条（不同角度/光照），检索时取最高相似度。"""
    __tablename__ = "face_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("face_enrollments.id", ondelete="CASCADE"), index=True)
    # float32 小端字节流，长度 = dim * 4。存 BLOB 而不是 JSON 是体积与解析速度考虑：
    # 5000 人 × 128 维，BLOB 约 2.5MB，JSON 约 12MB 且解析耗时高一个数量级。
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    dim: Mapped[int] = mapped_column(Integer, default=128)
    model_name: Mapped[str] = mapped_column(String(64), default="sface")
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="register")  # register / import
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConsentRecord(Base):
    """同意/撤回留痕。合规要求处理记录至少保存 3 年，因此只增不删。"""
    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(16), index=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(16))            # grant / revoke
    subject: Mapped[str] = mapped_column(String(16), default="self")  # self / guardian
    method: Mapped[str] = mapped_column(String(24), default="written")  # written / electronic / paper
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    evidence_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PiaRecord(Base):
    """个人信息保护影响评估（PIA）记录。

    《人脸识别技术应用安全管理办法》第九条要求事前开展评估并记录处理情况，
    且报告与记录至少保存 3 年；目的/方式变更或发生重大安全事件时要重新评估。
    这张表就是评估台账，页面上按到期时间提醒复评。
    """
    __tablename__ = "pia_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(32))              # 评估版本号，如 2026-Q3
    scope: Mapped[str] = mapped_column(Text)                      # 评估范围
    conclusion: Mapped[str] = mapped_column(Text, default="")     # 结论（合法、正当、必要）
    risks: Mapped[str | None] = mapped_column(Text, nullable=True)      # 风险点
    measures: Mapped[str | None] = mapped_column(Text, nullable=True)   # 降低影响的措施
    reviewer: Mapped[str] = mapped_column(String(64), default="")
    evidence_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 复评到期时间（一般一年一评）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """敏感数据访问审计：查询/导出/删除人员信息与生物特征时留痕。

    这是"数据最小化 + 可追溯"的落地手段：有了审计，才能回答
    "谁在什么时候查过某个学生的记录"这类合规问询。
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(48), index=True)   # person.read / face.enroll / face.revoke ...
    target_type: Mapped[str] = mapped_column(String(24), default="")   # student / teacher / staff / face
    target_id: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
