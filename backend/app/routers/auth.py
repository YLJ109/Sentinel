"""鉴权：登录（含失败限流）、当前用户、用户管理、改密、媒体 Cookie 签发。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.core.security import (create_access_token, create_media_token, hash_password,
                               verify_password)
from app.models import User
from app.schemas import LoginRequest, PasswordChange, Token, UserCreate, UserOut, UserUpdate

log = logging.getLogger("routers.auth")
router = APIRouter(prefix="/api/auth", tags=["鉴权"])

# 登录失败统计：(用户名, 客户端IP) -> [失败时间戳]；进程内实现，多实例部署需换 Redis
_failures: dict[tuple[str, str], list[float]] = {}
_locked: dict[tuple[str, str], float] = {}


def _client_ip(request: Request) -> str:
    """取客户端 IP 作为失败限流的分组键。

    默认只信任 TCP 对端地址：``X-Forwarded-For`` 由客户端随意伪造，
    若直接采信，攻击者每次换一个假 IP 就能无限重试（限流形同虚设）。
    仅在服务确实位于可信反向代理之后时，才把 CAB_TRUST_PROXY_HEADERS 置为 true。
    """
    if settings.TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_key(request: Request, username: str) -> tuple[str, str]:
    return (username.lower(), _client_ip(request))


def _ensure_not_locked(key: tuple[str, str]) -> None:
    until = _locked.get(key, 0.0)
    if until > time.time():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"登录失败次数过多，请 {int(until - time.time()) + 1} 秒后重试")


def _record_failure(key: tuple[str, str]) -> None:
    now = time.time()
    window = settings.LOGIN_FAILURE_WINDOW_SEC
    # 每次失败都顺带回收过期键：否则缓慢试错（始终不触发锁定）会让
    # (用户名, IP) 组合无限累积，形成无上限的内存增长与 O(n) 清理开销。
    if len(_failures) > 512:
        for k in [k for k, v in _failures.items() if not v or now - v[-1] >= window]:
            _failures.pop(k, None)

    hits = [t for t in _failures.get(key, []) if now - t < window]
    hits.append(now)
    _failures[key] = hits
    if len(hits) >= settings.LOGIN_MAX_FAILURES:
        _locked[key] = now + settings.LOGIN_LOCKOUT_SEC
        _failures.pop(key, None)
        log.warning("账号 %s 连续登录失败，已锁定 %s 秒", key[0], settings.LOGIN_LOCKOUT_SEC)
        # 顺手清理过期锁定条目
        cutoff = now - settings.LOGIN_LOCKOUT_SEC
        for k in [k for k, v in _locked.items() if v < cutoff]:
            _locked.pop(k, None)


def _clear_failures(key: tuple[str, str]) -> None:
    _failures.pop(key, None)
    _locked.pop(key, None)


def _issue_media_cookie(response: Response, user_id: int) -> None:
    """签发取证媒体 Cookie，使 <img>/<video> 无需自定义请求头即可访问。"""
    response.set_cookie(
        key=settings.MEDIA_COOKIE_NAME,
        value=create_media_token(user_id),
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=settings.MEDIA_COOKIE_SECURE,
        path="/api/media",
    )


# ---------------------------------------------------------------- 登录
@router.post("/login", response_model=Token)
async def login(body: LoginRequest, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)):
    key = _rate_key(request, body.username)
    _ensure_not_locked(key)

    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        _record_failure(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已禁用")

    _clear_failures(key)
    _issue_media_cookie(response, user.id)
    token = create_access_token(user.id, extra={"role": user.role})
    return Token(access_token=token)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(settings.MEDIA_COOKIE_NAME, path="/api/media")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------- 自助改密
@router.post("/password")
async def change_own_password(
    body: PasswordChange,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(body.old_password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")
    if len(body.new_password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码长度至少 8 位")
    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 用户管理（仅管理员）
@router.post("/users", response_model=UserOut)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码长度至少 8 位")
    exists = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    user = User(username=body.username, full_name=body.full_name, role=body.role,
                hashed_password=hash_password(body.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    return list((await db.execute(select(User).order_by(User.id))).scalars().all())


@router.patch("/users/{uid}", response_model=UserOut)
async def update_user(
    uid: int,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("admin")),
):
    user = await db.get(User, uid)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if user.id == admin.id and (body.disabled or (body.role and body.role != "admin")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用或降级当前登录的管理员")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.role is not None:
        # 角色合法性由 UserUpdate 的 Literal 类型在入参阶段保证，此处无需重复校验
        user.role = body.role
    if body.disabled is not None:
        user.disabled = body.disabled
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/users/{uid}/password")
async def reset_password(
    uid: int,
    body: PasswordChange,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    """管理员重置他人密码：只需 new_password。"""
    user = await db.get(User, uid)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if len(body.new_password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码长度至少 8 位")
    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    return {"ok": True}
