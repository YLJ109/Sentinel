"""全局配置（环境变量驱动，便于容器化部署）。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/app/core/config.py -> backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
EVIDENCE_DIR = DATA_DIR / "evidence"
CLIP_DIR = DATA_DIR / "clips"
MODELS_DIR = BASE_DIR / "models"

# 开发用默认密钥。生产环境（DEBUG=false）若未替换，服务将拒绝启动。
DEFAULT_SECRET_KEY = "change-me-in-production-please-use-32+chars"

# 各类异常行为的中文标签与报警级别
BEHAVIOR_LABELS: dict[str, dict[str, object]] = {
    "fall":   {"label": "跌倒",     "is_bullying": False, "level": "medium"},
    "smoke":  {"label": "疑似吸烟", "is_bullying": False, "level": "low"},
    "fight":  {"label": "打架",     "is_bullying": True,  "level": "high"},
    "argue":  {"label": "争吵",     "is_bullying": True,  "level": "medium"},
    "crowd":  {"label": "人员聚集", "is_bullying": False, "level": "low"},
    "person": {"label": "人员",     "is_bullying": False, "level": "low"},
    "normal": {"label": "正常",     "is_bullying": False, "level": "low"},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CAB_", extra="ignore")

    # ===== 基础 =====
    APP_NAME: str = "守望 · 校园反霸凌智能检测系统"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    # 运行环境标识。设为 production 时，下面若干"仅开发可用"的宽松配置会被强制校验
    ENV: str = "development"

    # ===== 安全 =====
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 12
    # 生产环境（DEBUG=false）若仍使用默认密钥则拒绝启动，避免令牌可伪造
    ALLOW_DEFAULT_SECRET_KEY: bool = False
    # 允许的前端来源（CORS）。生产必须显式列出，禁止 "*" 与 credentials 并用
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # 取证媒体（截图/视频/转写）走 HttpOnly Cookie 鉴权，便于 <img>/<video> 直接加载
    MEDIA_COOKIE_NAME: str = "cab_media"
    MEDIA_COOKIE_SECURE: bool = False      # 生产启用 HTTPS 后置为 true
    # 首次启动时自动创建的内置管理员口令。仅用于本地开箱体验，
    # 生产环境（ENV=production）仍为此默认值会拒绝启动，避免弱口令上线。
    INITIAL_ADMIN_PASSWORD: str = "admin123"

    # ===== 登录保护 =====
    LOGIN_MAX_FAILURES: int = 5            # 窗口内允许的失败次数
    LOGIN_FAILURE_WINDOW_SEC: int = 300    # 失败统计窗口
    LOGIN_LOCKOUT_SEC: int = 300           # 超限后的锁定时长
    # 是否信任 X-Forwarded-For 作为客户端 IP。默认关闭：
    # 该头由客户端自行伪造，开启后攻击者只要每次换一个 XFF 就能绕过登录失败锁定。
    # 仅当服务确实部署在可信反向代理之后时才置为 true。
    TRUST_PROXY_HEADERS: bool = False

    # ===== 数据库 =====
    SQLITE_PATH: str = str(DATA_DIR / "campus.db")

    # ===== 推理设备 =====
    DEVICE: str = "auto"                 # auto / cuda:0 / cpu
    HALF: bool = True                    # GPU 上启用 FP16 加速
    IMGSZ: int = 640                     # 推理输入尺寸
    MAX_DET: int = 60                    # 单帧最大目标数

    # ===== 视觉模型权重（缺失时自动下载）=====
    YOLO_DETECT_WEIGHTS: str = "yolov8n.pt"        # 人员检测基座
    YOLO_POSE_WEIGHTS: str = "yolov8n-pose.pt"     # 人体姿态估计
    YOLO_BEHAVIOR_WEIGHTS: str = ""                # 自定义行为模型（如 smoke/violence），存在即启用

    DETECT_CONF: float = 0.35
    POSE_CONF: float = 0.35
    IOU_THRESHOLD: float = 0.5
    TRACKER_CFG: str = "bytetrack.yaml"

    # ===== 人脸检测（仅定位人脸框，不做人脸识别/比对）=====
    ENABLE_FACE: bool = True
    # 留空：使用 OpenCV 自带 Haar 级联（随 opencv 安装、离线可用）；
    # 填入开源 YOLO 人脸权重名（放进 backend/models/）即自动升级为模型检测
    FACE_MODEL: str = ""
    FACE_CONF: float = 0.45
    FACE_MIN_RATIO: float = 0.06         # 最小人脸边长占画面短边比例（Haar 用）
    FACE_MAX_FACES: int = 30             # 单帧最多标注的人脸数

    # ===== 推理性能 =====
    INFER_FPS_LIMIT: int = 12            # 实时流每秒最多推理帧数（同一路多个观看者共享）
    ENABLE_POSE: bool = True             # 关闭可省算力（仅保留人员/聚集检测）
    # 运动门控：画面几乎静止时跳过推理，显著降低空场景算力占用
    MOTION_GATE_ENABLED: bool = True
    MOTION_DIFF_THRESHOLD: float = 1.2   # 灰度帧差均值（0~255）低于该值视为无活动
    MOTION_GATE_FORCE_INTERVAL_SEC: float = 2.0  # 无活动时也至少每隔这么久推理一帧
    # 视频文件分析的后台任务并发上限（GPU 显存有限，默认串行）
    VIDEO_WORKER_CONCURRENCY: int = 1

    # ===== 行为判定阈值（真实几何/运动学指标）=====
    FALL_ASPECT_RATIO: float = 1.05      # 外接框 宽/高 超过该值视为倒地姿态
    FALL_TORSO_ANGLE: float = 52.0       # 躯干与竖直方向夹角（度）
    FALL_HEAD_DROP: float = 0.55         # 鼻尖高度低于肩宽参考的阈值
    FALL_MIN_FRAMES: int = 5             # 连续命中帧数（去抖）

    FIGHT_DIST_RATIO: float = 1.75       # 两人中心距 / 平均身高
    FIGHT_WRIST_SPEED: float = 0.62      # 手腕归一化速度阈值（/秒）
    FIGHT_MIN_FRAMES: int = 4
    FIGHT_IOU_OVERLAP: float = 0.06      # 外接框交叠比（肢体纠缠）

    ARGUE_DIST_RATIO: float = 2.2        # 争吵：面对面且持续贴近
    ARGUE_MIN_FRAMES: int = 10

    SMOKE_HAND_HEAD_RATIO: float = 0.62  # 手腕-鼻尖距离 / 肩宽
    SMOKE_MIN_FRAMES: int = 8

    CROWD_MIN_PEOPLE: int = 4            # 同帧人数达到该值判定聚集

    EVENT_VOTE_WINDOW: int = 15          # 轨迹级投票窗口帧数
    TRACK_TTL_FRAMES: int = 45           # 轨迹失活回收帧数

    # ===== 语音识别（Provider 可插拔：云端为主，本地兜底）=====
    SPEECH_ENABLED: bool = True
    ASR_PROVIDER: str = "dashscope"      # dashscope（百炼） / aliyun（NLS） / vosk（本地离线兜底）
    SPEECH_SAMPLE_RATE: int = 16000
    SPEECH_MIN_CONFIDENCE: float = 0.35

    # --- 阿里云百炼（DashScope）Paraformer 实时识别 ---
    # 百炼控制台创建 API-KEY（sk- 开头）后填入
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_WS_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    DASHSCOPE_ASR_MODEL: str = "paraformer-realtime-v2"

    # --- 阿里云智能语音交互（NLS 实时识别）---
    ALIYUN_NLS_REGION: str = "cn-shanghai"
    ALIYUN_NLS_ENDPOINT: str = "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1"
    ALIYUN_TOKEN_ENDPOINT: str = "https://nls-meta.cn-shanghai.aliyuncs.com"
    ALIYUN_ACCESS_KEY_ID: str = ""
    ALIYUN_ACCESS_KEY_SECRET: str = ""
    ALIYUN_NLS_APPKEY: str = ""
    # 可选：由外部令牌服务签发时直接提供，免去在应用内放置 AccessKey
    ALIYUN_NLS_TOKEN: str = ""

    # --- Vosk 本地兜底（仅 ASR_PROVIDER=vosk 时下载与加载）---
    VOSK_MODEL_NAME: str = "vosk-model-small-cn-0.22"
    VOSK_MODEL_URL: str = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"

    # 霸凌/威胁类关键词
    KEYWORD_BULLYING: list[str] = [
        "打你", "打死你", "弄死你", "揍他", "打死他", "打他", "废物", "垃圾", "滚开",
        "不要脸", "贱人", "傻逼", "去死", "你给我", "叫你", "跪下", "交出来",
        "别理他", "孤立他", "扒光", "扇他", "踹他", "钱交出来",
    ]
    # 求救/求助类关键词
    KEYWORD_ALARM: list[str] = ["救命", "救救我", "报警", "老师", "help", "help me", "别打我"]

    # ===== 报警策略 =====
    ALARM_MIN_CONFIDENCE: float = 0.55
    ALARM_COOLDOWN_SEC: int = 8
    ALARM_MERGE_WINDOW_SEC: int = 5      # 同轨迹同类事件合并窗口
    # 冷却/去重状态落库，保证多 worker 与重启后行为一致
    ALARM_STATE_TTL_SEC: int = 3600

    # ===== 取证 =====
    EVIDENCE_KEEP_DAYS: int = 90
    CLIP_PRE_SECONDS: int = 3            # 报警前预录时长
    CLIP_POST_SECONDS: int = 5           # 报警后延录时长
    CLIP_FPS: int = 12
    EVIDENCE_JPEG_QUALITY: int = 88
    # 实时流预录环形缓冲的容量（帧），需 >= CLIP_PRE_SECONDS * CLIP_FPS
    CLIP_RING_CAPACITY: int = 48
    # 留存清理任务
    RETENTION_ENABLED: bool = True
    RETENTION_INTERVAL_SEC: int = 6 * 3600

    # ===== 报警外发通知（可选）=====
    # 企业微信/钉钉/自建网关的 Webhook；留空则不外发
    NOTIFY_WEBHOOK_URL: str = ""
    NOTIFY_TIMEOUT_SEC: float = 5.0
    NOTIFY_MIN_LEVEL: str = "high"       # 仅推送不低于该级别的报警（high / medium / low）

    # ===== 训练（自定义行为模型）=====
    TRAIN_DATASET_YAML: str = str(BASE_DIR / "datasets" / "behaviors" / "data.yaml")
    TRAIN_BASE_WEIGHTS: str = "yolov8n.pt"
    TRAIN_EPOCHS: int = 100
    TRAIN_BATCH: int = 16

    # ---------- 派生属性 ----------
    @property
    def sqlite_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.SQLITE_PATH}"

    @property
    def DATA_DIR(self) -> Path:
        return DATA_DIR

    @property
    def UPLOAD_DIR(self) -> Path:
        return UPLOAD_DIR

    @property
    def EVIDENCE_DIR(self) -> Path:
        return EVIDENCE_DIR

    @property
    def CLIP_DIR(self) -> Path:
        return CLIP_DIR

    @property
    def MODELS_DIR(self) -> Path:
        return MODELS_DIR

    @property
    def vosk_model_path(self) -> Path:
        return MODELS_DIR / self.VOSK_MODEL_NAME

    def ensure_dirs(self) -> None:
        for d in (DATA_DIR, UPLOAD_DIR, EVIDENCE_DIR, CLIP_DIR, MODELS_DIR):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()


def behavior_meta(event_type: str) -> dict[str, object]:
    """返回行为的中文标签 / 是否霸凌 / 报警级别。"""
    return BEHAVIOR_LABELS.get(event_type, BEHAVIOR_LABELS["normal"])
