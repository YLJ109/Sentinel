"""摄像头 / 视频源管理。

本机摄像头不做任何预置：由前端 ``enumerateDevices()`` 枚举真实设备后调用 ``/sync``
对齐摄像头表，保证「电脑上有几个摄像头就显示几个」，名称取设备真实标签。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.models import Camera, User
from app.schemas import CameraCreate, CameraOut, CameraSyncRequest, CameraUpdate

router = APIRouter(prefix="/api/cameras", tags=["摄像头"])

# 写操作统一要求 admin/operator：viewer 为只读角色，不得增删改点位
_write = require_role("admin", "operator")

# 自动发现时写入的占位名；带此特征的名称允许被设备真实标签覆盖，人工改名则保留
_AUTO_NAME = re.compile(r"^(本机摄像头|摄像头)\s*\d*$")


@router.get("", response_model=list[CameraOut])
async def list_cameras(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return list((await db.execute(select(Camera).order_by(Camera.id))).scalars().all())


@router.post("", response_model=CameraOut)
async def create_camera(
    body: CameraCreate, db: AsyncSession = Depends(get_db), _: User = Depends(_write),
):
    cam = Camera(**body.model_dump())
    db.add(cam)
    await db.commit()
    await db.refresh(cam)
    return cam


@router.post("/sync", response_model=list[CameraOut])
async def sync_devices(
    body: CameraSyncRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin", "operator")),
):
    """用浏览器枚举到的本机设备对齐摄像头表。

    只动「自动发现」的条目（``device_id`` 非空），人工新增的 RTSP / 文件源不受影响：
    - 设备在、库里没有 → 新增一行；
    - 两边都有 → 仅当库里名称仍是占位名时用真实标签覆盖（人工改名不被冲掉）；
    - 库里有、设备已不在 → 删除，保证条目数与真实设备数一致。

    空列表保护：浏览器在未授权或枚举失败时会回传空数组（甚至只有占位 id），
    此时若照常走"删掉所有未出现设备"的分支，会把全部自动发现点位清空。
    因此设备列表为空时只返回现状，不做任何删除。
    """
    rows = list((await db.execute(select(Camera))).scalars().all())
    if not body.devices:
        return rows

    known = {c.device_id: c for c in rows if c.device_id}
    seen: set[str] = set()

    for dev in body.devices:
        if not dev.device_id or dev.device_id in seen:
            continue
        seen.add(dev.device_id)
        label = (dev.label or "").strip()
        cam = known.get(dev.device_id)
        if cam is None:
            db.add(Camera(
                name=label or f"本机摄像头 {dev.index + 1}",
                location="",
                source_type="webcam",
                source_url="",
                device_id=dev.device_id,
                enabled=True,
            ))
        elif label and _AUTO_NAME.match((cam.name or "").strip()):
            cam.name = label

    for c in rows:
        if c.device_id and c.device_id not in seen:
            await db.delete(c)

    await db.commit()
    return list((await db.execute(select(Camera).order_by(Camera.id))).scalars().all())


@router.patch("/{cam_id}", response_model=CameraOut)
async def update_camera(
    cam_id: int, body: CameraUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(_write),
):
    cam = await db.get(Camera, cam_id)
    if cam is None:
        return _not_found()
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cam, k, v)
    await db.commit()
    await db.refresh(cam)
    return cam


@router.delete("/{cam_id}")
async def delete_camera(
    cam_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(_write),
):
    cam = await db.get(Camera, cam_id)
    if cam is None:
        return _not_found()
    await db.delete(cam)
    await db.commit()
    return {"ok": True}


def _not_found():
    raise HTTPException(status.HTTP_404_NOT_FOUND, "摄像头不存在")
