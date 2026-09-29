"""密码哈希与 JWT 工具。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 令牌用途声明：取证媒体读取专用
MEDIA_SCOPE = "media"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str | int, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode: dict[str, Any] = {"sub": str(subject), "exp": expire}
    if extra:
        to_encode.update(extra)
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_media_token(subject: str | int) -> str:
    """签发仅用于读取取证文件的令牌（写入 HttpOnly Cookie，供 <img>/<video> 使用）。"""
    return create_access_token(subject, extra={"scope": MEDIA_SCOPE})


def decode_access_token(
    token: str,
    require_scope: str | None = None,
    forbid_scopes: tuple[str, ...] = (),
) -> str | None:
    """解析令牌返回 subject。

    ``require_scope``：不为空时要求 scope 恰好等于该值（媒体路由使用）。
    ``forbid_scopes``：用途受限的 scope 黑名单。业务鉴权必须传 ``(MEDIA_SCOPE,)``，
    否则写进 HttpOnly Cookie 的取证媒体令牌可以直接当 Bearer 调用全部业务接口。
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    scope = payload.get("scope")
    if scope is not None and scope in forbid_scopes:
        return None
    if require_scope is not None and scope != require_scope:
        return None
    return payload.get("sub")
