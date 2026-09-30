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
    "fall":     {"label": "跌倒",     "is_bullying": False, "level": "medium"},
    "smoke":    {"label": "疑似吸烟", "is_bullying": False, "level": "low"},
    # fight 与 bullying 都是高等级，但语义不同：
    # fight = 对等冲突（互殴），bullying = 单向欺凌（力量/主动权高度不对等）。
    # 由 vision/behaviors.py 的互动对称性分析区分，详见 _interaction()。
    "fight":    {"label": "打架",     "is_bullying": True,  "level": "high"},
    "bullying": {"label": "欺凌",     "is_bullying": True,  "level": "high"},
    "argue":    {"label": "争吵",     "is_bullying": True,  "level": "medium"},
    "crowd":    {"label": "人员聚集", "is_bullying": False, "level": "low"},
    "person":   {"label": "人员",     "is_bullying": False, "level": "low"},
    "normal":   {"label": "正常",     "is_bullying": False, "level": "low"},
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
    # 三级优先，逐级回落，保证任何环境下都开箱可用：
    #   ① FACE_MODEL 填了 YOLO 人脸权重名 → 用 YOLO（精度最高、复用同一 GPU）
    #   ② 留空 → 自动使用 YuNet（OpenCV 自带 DNN 接口，权重约 230KB，
    #      官方基准下速度约为 Haar 的 5 倍，侧脸/遮挡召回也更好）
    #   ③ YuNet 权重缺失 → 回落 OpenCV Haar 级联（随 opencv 安装，完全离线）
    FACE_MODEL: str = ""
    # YuNet 权重文件名（放在 backend/models/ 下）。换其他版本时改这里即可
    FACE_YUNET_MODEL: str = "face_detection_yunet_2023mar.onnx"
    FACE_CONF: float = 0.45
    FACE_MIN_RATIO: float = 0.06         # 最小人脸边长占画面短边比例（Haar 用）
    FACE_MAX_FACES: int = 30             # 单帧最多标注的人脸数

    # ===== 人脸识别（1:N 身份检索，与上面的"仅检测"是两件事）=====
    # 引擎选用 OpenCV SFace（Apache 2.0，可商用）：特征 128 维、权重约 37MB，
    # 与已用的 YuNet 同源，且 YuNet 输出的 5 个人脸关键点正好是 SFace
    # alignCrop() 需要的对齐输入 —— 无需新增检测模型、无需手写相似变换。
    ENABLE_FACE_ID: bool = True
    FACE_SFACE_MODEL: str = "face_recognition_sface_2021dec.onnx"
    FACE_RECOGNIZE_EVERY: int = 3        # 每 N 次检测做一次识别（识别远贵于检测）
    FACE_RECOGNIZE_MAX: int = 6          # 单帧最多识别张数（按人脸框面积优先取大脸）
    FACE_MATCH_THRESHOLD: float = 0.42   # 余弦相似度阈值（SFace 官方 0.363，此处更保守）
    # top1 与 top2 的最小间隔：两家相似度接近时说明存在混淆（双胞胎、相似衣着），
    # 此时返回"不确定"比猜一个名字更安全 —— 张冠李戴在校园场景是严重事故。
    FACE_MATCH_MARGIN: float = 0.06
    FACE_CONFIRM_VOTES: int = 3          # 轨迹级确认票数（连续 N 次识别一致才写姓名）
    FACE_MIN_FACE_PX: int = 100          # 注册照人脸最小边长（像素）
    FACE_MIN_SHARPNESS: float = 60.0     # 注册照清晰度下限（Laplacian 方差）
    FACE_MAX_POSE_RATIO: float = 0.45    # 正脸约束：双眼中点相对人脸中心的水平偏移上限
    FACE_PHOTO_KEEP: bool = True         # 是否保留注册原图（关闭则只留特征与头像）

    # ===== 情绪识别（FER+，纯实时展示，不落库）=====
    # 情绪只作为值班台的实时辅助线索：不写入学生档案、不参与任何评价，
    # 界面必须标注"仅供参考，非心理诊断"。落库开关刻意不提供 —— 见合规设计。
    ENABLE_EMOTION: bool = True
    EMOTION_MODEL: str = "emotion-ferplus-8.onnx"
    EMOTION_EVERY: int = 2               # 每 N 次人脸识别附带做一次情绪
    EMOTION_CONF_MIN: float = 0.40       # 低于该置信度归为"中性/不确定"
    EMOTION_EMA_ALPHA: float = 0.25      # 轨迹级情绪概率的滑动平均系数
    EMOTION_VOTE_WINDOW: int = 8         # 多数投票窗口（帧）

    # ===== 人员与人脸数据留存 =====
    # 人脸注册照与头像目录（位于本机 data/ 下，按合规要求不上传互联网）
    FACE_PHOTO_DIRNAME: str = "faces"

    # ===== 推理性能 =====
    # 实时流每秒最多推理帧数（同一路多个观看者共享该限频窗口）。
    # 前端固定按 10fps（每 100ms 一帧）送检，这里留到 12 是刻意为之：
    #   ① 两个观看者同看一路时总送帧率会到 20fps，超出的部分由共享窗口丢弃；
    #   ② 若把两者调成相等，调度抖动会让单页正常观看也偶发丢帧。
    INFER_FPS_LIMIT: int = 12
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

    ARGUE_DIST_RATIO: float = 1.1        # 争吵：必须贴得很近。
                                         # 原值 2.2（约 3.7m）过宽，会让并肩走路、
                                         # 排队等一切正常近距离场景全部命中
    ARGUE_MIN_FRAMES: int = 10
    # 争吵必须存在「对峙姿态」或「轻微肢体动作」，否则两人只是并列站着
    # （排队、并肩同行）也会被判成争吵 —— 这是实测发现的误报来源
    ARGUE_FACING_MIN: float = 0.35       # 至少一方肩线明显朝向对方
    ARGUE_MIN_MOTION: float = 0.12       # 或存在手势/前倾等轻微动作

    # ===== 欺凌判别：互动对称性分析 =====
    # 打斗/推搡成立之后，再判断它是"单向欺凌"还是"对等冲突/嬉闹"。
    # 常见方案只判"距离近 + 手部动作快"，因此并肩走路、拍肩、课间打闹都会误报；
    # 这里用四个可解释的"不对等程度"指标来区分（全部来自已有骨架与轨迹）：
    #   ① 运动强度不对称：一方挥臂猛烈、另一方几乎不动
    #   ② 退缩不对称：一方持续逼近、另一方持续后撤
    #   ③ 追逃模式：同一个人既被追又在逃（不同人分工）
    #   ④ 压制姿态：贴近时一方头部低于另一方肩线
    BULLY_SCORE_THRESHOLD: float = 0.52    # 累积分数超过该值判为欺凌，否则判为对等冲突
    BULLY_ASYM_WEIGHT: float = 0.34
    BULLY_FLEE_WEIGHT: float = 0.30
    BULLY_CHASE_WEIGHT: float = 0.22
    BULLY_SUPPRESS_WEIGHT: float = 0.14

    # ===== 群体欺凌：多人同时对同一人动手 =====
    # 真实校园霸凌最常见的形态是"几个人围住一个"，而两两对称性分析在这种场景
    # 必然失效（攻击者彼此看都是对等冲突）。这里用"跨帧打斗关系图"补上这一情形。
    GROUP_BULLY_MIN_ATTACKERS: int = 2      # 同时卷入的人数下限（星型图的邻接度）
    GROUP_BULLY_MAX_COHESION: float = 0.34  # 这些同伴之间的对峙边密度上限（星型图判据）
    GROUP_BULLY_EDGE_FRAMES: int = 2        # 一对人近窗内打斗达到几帧才算连边
    GROUP_BULLY_MIN_FRAMES: int = 3         # 连续确认帧数
    BULLY_EMA_ALPHA: float = 0.25          # 交互指标的滑动平均系数（抑制单帧抖动）
    BULLY_MIN_FRAMES: int = 3              # 判定生效所需的连续帧数
    # 嬉闹抑制：四项指标同时"对等且无退缩"时，判为嬉闹，不产生报警。
    # 这是降低误报的关键闸门 —— 课间追逐打闹是本场景最主要的误报来源。
    PLAY_ASYM_MAX: float = 0.30            # 运动强度需足够对称
    PLAY_RETREAT_MAX: float = 0.18         # 无明显单向后撤
    PLAY_CHASE_MAX: float = 0.25           # 无明显追逃分工
    PLAY_SUPPRESS_MAX: float = 0.50        # 无明显压制姿态

    SMOKE_HAND_HEAD_RATIO: float = 0.62  # 手腕-鼻尖距离 / 肩宽
    SMOKE_MIN_FRAMES: int = 8

    CROWD_MIN_PEOPLE: int = 4            # 同帧人数达到该值判定聚集

    # ===== 视频文件检测的自适应采样 =====
    # 固定间隔会整段跳过短促动作：实测 8 秒素材里的推搡只持续约 0.35 秒，
    # 用 0.33 秒的固定步长恰好落在两点之间，打人动作完全消失（只剩围观与倒地）。
    # 改为按画面运动自适应：静止稀疏、动作密集并保持一小段活跃期。
    #
    # 注意 VIDEO_SAMPLE_ACTIVE_SEC 与行为判定的"确认帧数"是联动的：
    # 打架需要连续 4 帧命中（FIGHT_MIN_FRAMES），因此动作时段的采样间隔
    # 必须让一段 0.3~0.4 秒的短促冲突至少采到 4 帧。0.07 秒即 5 帧，
    # 留出余量；调大该值会重新引入短促动作漏报。
    VIDEO_MOTION_THRESHOLD: float = 0.010   # 帧差运动量阈值（160x90 灰度归一化）
    VIDEO_SAMPLE_ACTIVE_SEC: float = 0.07   # 有动作时的采样间隔
    VIDEO_SAMPLE_IDLE_SEC: float = 0.5      # 静止时的采样间隔
    VIDEO_ACTIVE_HOLD_SEC: float = 0.8      # 检测到动作后维持密集采样的时长

    EVENT_VOTE_WINDOW: int = 15          # 轨迹级投票窗口帧数
    TRACK_TTL_FRAMES: int = 45           # 轨迹失活回收帧数（兜底上限，正常按时间回收）
    TRACK_TTL_SEC: float = 0.8           # 轨迹失活回收时长（秒）：帧率变化时存活时长不变
    TRACK_CONFIRM_HITS: int = 2          # 轨迹确认门控：连续命中该帧数后才对外可见
    TRACK_MAX_EXTRAP: int = 15           # 连续外推上限：超过则冻结预测框，防止甩出画面

    # ===== 人数统计（中位数滤波 + 不对称迟滞）=====
    PEOPLE_WINDOW: int = 5               # 中位数滤波窗口帧数
    PEOPLE_UP_HOLD: int = 3              # 人数上调需连续确认帧数
    PEOPLE_DOWN_HOLD: int = 8            # 人数下调需连续确认帧数（遮挡不应立刻减人）

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

    # ===== 多模态证据融合（视觉与语音时间窗内互相印证）=====
    # 视觉与语音在时间上互相印证时，报警可信度显著高于单一模态：
    # 视觉误判会被语音证伪，语音幻听也会被视觉证伪。这是"如何降低误报"
    # 最有力的回答，也是本项目区别于单模态方案的核心设计。
    FUSION_ENABLED: bool = True
    # 关联时间窗。语音是成句后才转写出来的，从说话到出结果通常有 2~5 秒延迟，
    # 窗口定得太小（如 3 秒）会让互证几乎永远命中不了，等于功能失效。
    FUSION_WINDOW_SEC: float = 12.0
    FUSION_BOOST: float = 0.15           # 双向印证时的置信度加权
    FUSION_REQUIRE_BOTH: bool = False    # 是否要求视觉与语音同时命中才报警

    # ===== 报警策略 =====
    ALARM_MIN_CONFIDENCE: float = 0.55
    ALARM_COOLDOWN_SEC: int = 8
    ALARM_MERGE_WINDOW_SEC: int = 5      # 同轨迹同类事件合并窗口
    # 冷却/去重状态落库，保证多 worker 与重启后行为一致
    ALARM_STATE_TTL_SEC: int = 3600

    # ===== 隐私保护 =====
    # 隐私遮蔽区域：目标框中心落在区域内的，不产生检测事件、不落任何取证。
    # 用于教室后墙、卫生间门口等不宜纳入监测的位置（数据最小化原则）。
    # 格式为 JSON 数组，每项 {"camera": 点位 id 或 null 表示全部点位,
    #                        "rect": [x1, y1, x2, y2]}，坐标为 0~1 归一化像素坐标。
    # 示例：[{"camera": 3, "rect": [0.62, 0.0, 1.0, 0.55]}]
    PRIVACY_ZONES: str = "[]"

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
    def FACE_DIR(self) -> Path:
        """人脸注册照与头像目录（本机存储，不外传）。"""
        return DATA_DIR / self.FACE_PHOTO_DIRNAME

    @property
    def MODELS_DIR(self) -> Path:
        return MODELS_DIR

    @property
    def vosk_model_path(self) -> Path:
        return MODELS_DIR / self.VOSK_MODEL_NAME

    def ensure_dirs(self) -> None:
        for d in (DATA_DIR, UPLOAD_DIR, EVIDENCE_DIR, CLIP_DIR, MODELS_DIR, self.FACE_DIR):
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
