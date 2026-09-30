"""取证媒体访问（截图 / 标注视频 / 转写文本）。

安全设计：
- 前端不再直接挂载静态目录，所有取证文件都经过本路由鉴权后返回。
- 鉴权支持两种方式：HttpOnly Cookie（`cab_media`，供 `<img>` / `<video>` 直接加载）
  与 `Authorization: Bearer`（供脚本、导出、第三方集成调用）。
- Cookie 中的令牌带 ``scope=media`` 声明，即使泄露也无法调用业务接口。
- 路径做规范化与根目录校验，阻断 ``../`` 目录穿越。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import MEDIA_SCOPE, decode_access_token
from app.models import User

router = APIRouter(prefix="/api/media", tags=["取证媒体"])

# 仅允许读取这些子目录，避免把数据库文件等暴露出去。
# faces 目录存放人脸注册照与头像 —— 它同样必须走鉴权，绝不能做成静态目录：
# 否则只要知道文件名就能绕过权限直接拉走学生照片。
ALLOWED_ROOTS = ("evidence", "clips", "uploads", "faces")


def _resolve_safe(rel_path: str) -> Path:
    """把相对路径解析到 DATA_DIR 内，并校验未被越界。"""
    root = Path(settings.DATA_DIR).resolve()
    candidate = (root / rel_path.lstrip("/\\")).resolve()
    if root != candidate and root not in candidate.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法路径")
    try:
        rel_first = candidate.relative_to(root).parts[0]
    except (ValueError, IndexError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法路径") from None
    if rel_first not in ALLOWED_ROOTS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该目录不允许访问")
    if not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    return candidate


async def _authorize(request: Request, db: AsyncSession) -> User:
    """Cookie 或 Bearer 任一通过即可。"""
    token = request.cookies.get(settings.MEDIA_COOKIE_NAME)
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少身份凭证")

    # 媒体 Cookie 必须带 media scope；业务 Bearer 令牌（无 scope）也放行
    user_id = decode_access_token(token, require_scope=MEDIA_SCOPE)
    if user_id is None:
        user_id = decode_access_token(token)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效或过期的凭证")

    user = await db.get(User, int(user_id))
    if user is None or user.disabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    return user


@router.get("/{rel_path:path}")
async def get_media(rel_path: str, request: Request, db: AsyncSession = Depends(get_db)):
    await _authorize(request, db)
    path = _resolve_safe(rel_path)
    mime, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=mime or "application/octet-stream",
        filename=path.name,
        # 取证文件按需刷新，不做长缓存，避免删除后仍可访问
        headers={"Cache-Control": "private, max-age=300"},
    )
