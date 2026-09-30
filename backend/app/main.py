"""FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.audio.asr import asr
from app.core.config import settings
from app.core.db import init_db
from app.core.logbuffer import install as install_log_buffer
from app.core.security import hash_password
from app.models import Camera, User
from app.routers import (alarms, auth, cameras, dashboard, detect, history, keywords,
                         media, risk, system, video)
from app.vision.engine import engine
from app.vision.registry import registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("app")

# 挂载进程内日志环形缓冲：控制台可直接查看后端异常，无需登录服务器
log_buffer = install_log_buffer()

# 是否生产环境：同时决定配置校验强度与交互式文档是否开放
_IS_PROD = settings.ENV.strip().lower() in ("production", "prod")

# 历史版本内置的演示点位：启动时清理，避免把编造的数据当成真实点位展示
LEGACY_DEMO_CAMERAS = [
    ("教学楼A栋走廊", "rtsp://192.168.1.101:554/stream1"),
    ("操场西北角", "rtsp://192.168.1.102:554/stream1"),
    ("食堂一层入口", "rtsp://192.168.1.103:554/stream1"),
    ("宿舍楼连廊", ""),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_secure_config()
    await init_db()
    await _seed_admin()
    await _purge_demo_cameras()
    await _load_runtime_config()
    await _seed_keywords()
    await _warmup()
    tasks = _start_background_tasks()
    yield
    for t in tasks:
        t.cancel()


async def _seed_keywords() -> None:
    """首次启动把内置词库灌入数据库，并载入内存缓存。

    必须在 warmup 之前完成：词表为空会让关键词检测**静默失效**
    （一条都不命中，且不报任何错），这类问题极难排查。
    库中已有词条时 ``ensure_seeded`` 直接返回，不做全量比对。
    """
    from app.core.db import SessionLocal
    from app.services import keyword_store

    async with SessionLocal() as db:
        added = await keyword_store.ensure_seeded(db)
        count = await keyword_store.refresh(db)
    if added:
        log.info("关键词词库初始化完成：新增 %d 条，当前生效 %d 条", added, count)


async def _load_runtime_config() -> None:
    """载入界面上保存过的能力开关与推理设备，保证重启后行为一致。"""
    from app.core.runtime_config import runtime_config

    await runtime_config.load()
    # 界面上切换过设备时以库里的为准；warmup 会按该设备加载模型
    if runtime_config.device_locked:
        registry.use_device(runtime_config.device)


def _assert_secure_config() -> None:
    """生产环境（CAB_ENV=production）拒绝不安全配置；开发环境仅告警。

    这样既不破坏本地开发体验，又能保证上线时忘改密钥/放开 CORS 会被直接拦下。
    """
    from app.core.config import DEFAULT_SECRET_KEY

    is_prod = _IS_PROD
    default_key = settings.SECRET_KEY == DEFAULT_SECRET_KEY
    wildcard_cors = "*" in settings.CORS_ORIGINS

    if is_prod:
        problems: list[str] = []
        if default_key:
            problems.append("SECRET_KEY 仍为默认值，请设置 CAB_SECRET_KEY 为 32 位以上随机串")
        if wildcard_cors:
            problems.append("CORS_ORIGINS 不允许通配符 '*'，请显式列出前端域名")
        if not settings.MEDIA_COOKIE_SECURE:
            problems.append("MEDIA_COOKIE_SECURE 应为 true（HTTPS 部署下媒体 Cookie 需 Secure）")
        if settings.INITIAL_ADMIN_PASSWORD == "admin123":
            problems.append("内置管理员仍为默认弱口令，请设置 CAB_INITIAL_ADMIN_PASSWORD")
        if problems:
            raise RuntimeError("生产环境配置校验失败：" + "；".join(problems))
        log.info("生产环境配置校验通过")
        return

    if default_key:
        log.warning("当前使用默认 SECRET_KEY，仅可用于开发。上线前请设置 CAB_ENV=production 与 CAB_SECRET_KEY")
    if wildcard_cors:
        log.warning("CORS_ORIGINS 含通配符，生产环境会拒绝启动")


def _start_background_tasks() -> list[asyncio.Task]:
    """启动周期任务：留存清理、运行时状态回收。"""
    from app.services.retention import retention_loop
    from app.services.state_store import state_store

    tasks: list[asyncio.Task] = []
    if settings.RETENTION_ENABLED:
        tasks.append(asyncio.create_task(retention_loop(), name="retention"))
    tasks.append(asyncio.create_task(state_store.gc_loop(), name="state-gc"))
    return tasks


async def _seed_admin() -> None:
    """仅在库中不存在 admin 时创建首个管理员（口令取自配置，生产已强制非默认值）。"""
    from sqlalchemy import select

    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        exists = (await db.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
        if exists is None:
            db.add(User(username="admin", full_name="系统管理员", role="admin",
                        hashed_password=hash_password(settings.INITIAL_ADMIN_PASSWORD)))
            await db.commit()
            if settings.INITIAL_ADMIN_PASSWORD == "admin123":
                log.warning("已创建内置管理员 admin/admin123，仅限开发环境，请尽快修改口令")


async def _purge_demo_cameras() -> None:
    """删除历史版本内置的演示点位（名称与源地址完全匹配才删，不误伤真实配置）。"""
    from sqlalchemy import select

    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        rows = list((await db.execute(select(Camera))).scalars().all())
        removed = 0
        for cam in rows:
            if (cam.name, cam.source_url or "") in LEGACY_DEMO_CAMERAS and not cam.device_id:
                await db.delete(cam)
                removed += 1
        if removed:
            await db.commit()
            log.info("已清理 %d 个内置演示点位", removed)


async def _warmup() -> None:
    """预热视觉模型（CUDA 上下文与 kernel 编译），并尝试就绪语音引擎。

    预热失败只记录日志、不阻断启动——业务功能（鉴权/历史/处置）应保持可用，
    引擎状态通过 ``/api/system/models`` 暴露给前端。
    语音侧给 15s 超时：云端鉴权通常几百毫秒即可完成，而本地模型下载可能很久，
    超时后不阻塞启动，前端连接时再按需重试。
    """
    try:
        await asyncio.to_thread(engine.warmup)
    except Exception as e:
        log.error("视觉模型预热失败：%s", e)

    if not settings.SPEECH_ENABLED:
        log.info("语音识别已关闭（CAB_SPEECH_ENABLED=false）")
        return
    try:
        await asyncio.wait_for(asr.prepare(), timeout=15)
        log.info("语音识别就绪：%s", asr.status().get("provider"))
    except Exception as e:
        log.warning("语音识别暂未就绪（前端连接时会重试）：%s", e)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="守望 Sentinel · 校园反霸凌智能检测系统：实时/视频行为检测 + 语音关键词 + 报警取证",
    lifespan=lifespan,
    # 生产环境关闭交互式文档：/docs、/redoc、/openapi.json 匿名可访问，
    # 会把全部接口、参数与数据模型直接摊开给攻击者看，便于其枚举攻击面。
    docs_url=None if _IS_PROD else "/docs",
    redoc_url=None if _IS_PROD else "/redoc",
    openapi_url=None if _IS_PROD else "/openapi.json",
)
# 运行时长等状态以应用启动时间为基准
app.state.started_at = time.time()

app.add_middleware(
    CORSMiddleware,
    # 显式来源白名单：allow_credentials 与通配符不能并用
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(cameras.router)
app.include_router(detect.router)
app.include_router(video.router)
app.include_router(alarms.router)
app.include_router(history.router)
app.include_router(dashboard.router)
app.include_router(system.router)
app.include_router(media.router)
# 事前预警（基于历史事件的风险画像，与实时报警互补）
app.include_router(risk.router)
# 关键词管理（词表增删改查 + 三档响应）
app.include_router(keywords.router)

# 取证文件不再以静态目录匿名暴露，统一经 /api/media/{path} 鉴权后返回


@app.get("/api/health")
async def health():
    """存活探针。

    只返回最小信息：该接口匿名可访问（供探活/负载均衡使用），
    此前把推理设备、FP16、语音就绪状态一并吐出，等于免费给攻击者做指纹识别；
    这些信息现在只在需要鉴权的 /api/system/status 中提供。
    """
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)
