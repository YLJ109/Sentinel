"""视频文件检测：上传、后台逐帧真实推理、进度与结果查询。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.models import User, VideoRecord
from app.schemas import VideoRecordOut
from app.services.video_pipeline import process_video

log = logging.getLogger("routers.video")
router = APIRouter(prefix="/api/video", tags=["视频检测"])

ALLOWED_SUFFIX = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
MAX_UPLOAD_MB = 512
_CHUNK = 1024 * 1024

# 上传会落盘大文件并占用 GPU 推理队列，属于写操作：viewer 只读角色不得触发
_uploader = require_role("admin", "operator")


@router.post("/upload", response_model=VideoRecordOut)
async def upload_video(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    camera_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_uploader),
):
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少文件名")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"仅支持 {'/'.join(sorted(s.lstrip('.') for s in ALLOWED_SUFFIX))} 视频")

    saved = settings.UPLOAD_DIR / f"v_{int(time.time() * 1000)}{suffix}"

    # 分块落盘并做体积校验，避免一次性读入内存
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with saved.open("wb") as fp:
            while chunk := await file.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    fp.close()
                    saved.unlink(missing_ok=True)
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        f"文件超过 {MAX_UPLOAD_MB}MB 限制")
                fp.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        saved.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"文件写入失败：{e}")

    if written == 0:
        saved.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传内容为空")

    rec = VideoRecord(camera_id=camera_id, filename=file.filename, original_path=str(saved),
                      uploaded_by=user.id, status="processing", progress=0.0)
    db.add(rec)
    await db.commit()
    await db.refresh(rec)

    background.add_task(process_video, rec.id)
    return rec


@router.get("", response_model=list[VideoRecordOut])
async def list_videos(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return list((await db.execute(select(VideoRecord).order_by(VideoRecord.id.desc()))).scalars().all())


@router.get("/{vid}", response_model=VideoRecordOut)
async def get_video(vid: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    rec = await db.get(VideoRecord, vid)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记录不存在")
    return rec
