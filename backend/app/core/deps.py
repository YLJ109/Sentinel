"""依赖注入：当前用户解析。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import MEDIA_SCOPE, decode_access_token
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少身份凭证")
    # 业务接口一律拒绝取证媒体令牌：它只写进 HttpOnly Cookie 供 <img>/<video> 使用，
    # 若不在此拦截，一个媒体 Cookie 就能当全权 Bearer 调所有业务接口。
    user_id = decode_access_token(creds.credentials, forbid_scopes=(MEDIA_SCOPE,))
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效或过期的令牌")
    user = await db.get(User, int(user_id))
    if user is None or user.disabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    return user


def require_role(*roles: str):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
        return user
    return checker
