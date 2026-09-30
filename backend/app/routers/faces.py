"""人脸建档与识别：单图注册、1:N 检索、索引维护。

**"一张图片完成建档"的完整链路**：

    上传图片
      → YuNet 检测（多脸拒绝 / 可指定取最大脸）
      → 质量门（尺寸 / 清晰度 / 亮度 / 正脸 / 五官几何）
      → SFace 对齐 + 128 维特征
      → 1:N 检索（疑似重复提示，避免同一人被录两次）
      → 落盘注册照与头像 → 写库 → 重建内存索引

**为什么注册要传 consent_subject**：按《人脸识别技术应用安全管理办法》，
处理人脸信息需取得单独同意，不满 14 周岁还需监护人同意。把同意确认放在
建档这一步而不是事后补登，是为了让"未授权即无特征"成为**结构上的必然** ——
没有同意就无法产生 enrollment，也就不会有模板进入检索索引。
"""
from __future__ import annotations

import logging
import time
from io import BytesIO
from pathlib import Path

import numpy as np
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     UploadFile, status)
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.models import FaceTemplate, User
from app.schemas import FaceEnrollResult
from app.services import person_store
# 必须导入单例对象：services/face_index.py 的模块名与实例名同名，
# 用 ``from app.services import face_index`` 拿到的是模块，调用 status/search 会 AttributeError
from app.services.face_index import face_index
from app.vision.face import face_detector
from app.vision.face_id import face_id

log = logging.getLogger("routers.faces")

router = APIRouter(prefix="/api/faces", tags=["人脸建档与识别"])

_write = require_role("admin", "operator")

MAX_UPLOAD = 8 * 1024 * 1024        # 单张图片上限 8MB
PHOTO_MAX_SIDE = 720                # 原始注册照保存时的最长边（够复核，体积可控）
AVATAR_SIDE = 160                   # 头像边长（前端卡片展示用）


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _decode(raw: bytes) -> np.ndarray:
    """上传字节 → BGR ndarray（与视觉链路其余部分统一 BGR）。"""
    try:
        rgb = np.array(Image.open(BytesIO(raw)).convert("RGB"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片无法解析，请上传 JPG / PNG") from e
    if rgb.size == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片内容为空")
    return rgb[:, :, ::-1].copy()


async def _read_image(file: UploadFile) -> np.ndarray:
    raw = await file.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未收到文件内容")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片过大（上限 8MB）")
    return _decode(raw)


def _pick_face(faces: list[dict], pick: str) -> tuple[dict | None, str]:
    """从检测结果中选出用于建档/识别的那张脸。返回 (face, 错误原因)。"""
    if not faces:
        return None, "未检测到人脸，请换一张正面清晰的照片"
    if len(faces) == 1:
        return faces[0], ""
    if pick == "largest":
        # 合影场景下取面积最大的那张；面积用归一化框计算，与分辨率无关
        def area(f: dict) -> float:
            b = f["bbox"]
            return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

        return max(faces, key=area), ""
    return None, f"检测到 {len(faces)} 张人脸，请上传单人照片（或勾选「取画面中最大人脸」）"


def _save_images(owner_type: str, owner_id: int, img: np.ndarray,
                 aligned: np.ndarray | None) -> tuple[str, str]:
    """保存注册原图与头像，返回 (photo_path, avatar_path)。

    图片全部落在本机 data/faces 下，不经过任何网络传输。
    """
    import cv2

    out_dir = Path(settings.FACE_DIR) / owner_type
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)

    photo_path = ""
    if settings.FACE_PHOTO_KEEP:
        h, w = img.shape[:2]
        scale = min(1.0, PHOTO_MAX_SIDE / max(h, w))
        photo = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale)))) if scale < 1 else img
        p = out_dir / f"{owner_id}_{stamp}.jpg"
        # quality 用取证的 JPEG 质量设置：既要能看清人脸，又不至于单张几百 KB
        cv2.imwrite(str(p), photo, [int(cv2.IMWRITE_JPEG_QUALITY), settings.EVIDENCE_JPEG_QUALITY])
        photo_path = str(p)

    avatar_path = ""
    if aligned is not None:
        p = out_dir / f"{owner_id}_{stamp}_avatar.jpg"
        avatar = cv2.resize(aligned, (AVATAR_SIDE, AVATAR_SIDE))
        cv2.imwrite(str(p), avatar, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        avatar_path = str(p)

    return photo_path, avatar_path


def _person_brief(summary: dict | None) -> dict | None:
    if not summary:
        return None
    return {
        "owner_type": summary.get("owner_type"),
        "person_id": summary.get("person_id"),
        "name": summary.get("name"),
        "no": summary.get("no"),
        "gender": summary.get("gender"),
        "class_name": summary.get("class_name"),
        "grade": summary.get("grade"),
        "type_label": summary.get("type_label"),
        "avatar_url": summary.get("avatar_url"),
    }


# ---------------------------------------------------------------- 状态
@router.get("/status")
async def faces_status(_: User = Depends(get_current_user)) -> dict:
    """人脸能力的就绪状态。

    检测器、识别模型、索引三者**各自独立降级**：任一缺失都只影响对应能力，
    不会让整条检测链路不可用。前端据此提示"需要下载哪个权重文件"。
    """
    return {
        "detector": face_detector.status(),
        "identity": face_id.status(),
        "index": face_index.status(),
        "config": {
            "threshold": settings.FACE_MATCH_THRESHOLD,
            "margin": settings.FACE_MATCH_MARGIN,
            "confirm_votes": settings.FACE_CONFIRM_VOTES,
            "recognize_every": settings.FACE_RECOGNIZE_EVERY,
            "min_face_px": settings.FACE_MIN_FACE_PX,
        },
    }


@router.post("/reindex")
async def reindex(db: AsyncSession = Depends(get_db), actor: User = Depends(_write)) -> dict:
    """手动重建内存索引（换模型、批量导入特征后使用）。"""
    count = await face_index.rebuild(db)
    await person_store.audit(db, action="face.reindex", detail=f"{count} 条模板", actor=actor)
    return {"templates": count, "index": face_index.status()}


# ---------------------------------------------------------------- 单图注册
@router.post("/enroll", response_model=FaceEnrollResult)
async def enroll(
    owner_type: str = Form(...),
    person_id: int = Form(...),
    consent_subject: str = Form("self"),
    consent_method: str = Form("electronic"),
    replace: bool = Form(False),
    pick: str = Query("strict", pattern="^(strict|largest)$"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    """用**一张图片**完成人脸建档（顺带登记同意）。

    ``replace=True`` 时先清空旧模板再写入（补拍更清晰的照片）；默认追加，
    允许一个人积累 1~3 张不同角度的模板以提升识别稳定性。
    """
    if owner_type not in person_store.OWNER_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知人员类型")
    if consent_subject not in ("self", "guardian"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "同意主体只能是 self 或 guardian")

    person = await person_store.get_person(db, owner_type, int(person_id))
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人员不存在，请先建立人员档案")

    if not face_id.ready:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"人脸识别模型未就绪：{face_id.status()['error']}")

    img = await _read_image(file)
    faces = face_detector.detect(img)
    face, reason = _pick_face(faces, pick)
    if face is None:
        return {"ok": False, "reasons": [reason], "person": person}

    report = face_id.quality(img, face)
    if not report.ok:
        # 质量不合格直接拒绝：勉强入库的模板会在很长时间里持续制造误识，
        # 而且很难定位到"是当初那张照片拍糊了"。
        return {"ok": False, "quality": report.as_dict(), "reasons": report.reasons, "person": person}

    aligned = face_id.align(img, np.asarray(face["raw"], dtype=np.float32))
    vec = face_id.feature_from_aligned(aligned) if aligned is not None else None
    if vec is None:
        return {"ok": False, "quality": report.as_dict(),
                "reasons": ["特征提取失败，请换一张正面清晰的照片"], "person": person}

    duplicate = None
    # 疑似重复提示：相似度落在"明显像同一个人"的区间就提醒，避免同一学生被录两次。
    # 阈值取匹配阈值的 0.75 倍 —— 低于它的相似度在底库里相当普遍，提示只会变成噪声。
    for cand in face_index.search(vec, top_k=1):
        if (cand["owner_type"] != owner_type or int(cand["owner_id"]) != int(person_id)) \
                and cand["score"] >= settings.FACE_MATCH_THRESHOLD * 0.75:
            duplicate = {"score": round(float(cand["score"]), 4),
                         **(_person_brief(cand["summary"]) or {})}

    enroll_row = await person_store.get_enrollment(db, owner_type, int(person_id), create=True)
    assert enroll_row is not None

    if replace:
        await person_store.purge_face(db, owner_type, int(person_id))

    if enroll_row.consent_status != "granted":
        # 同意与建档在同一次调用里完成：把"必须先有同意"变成流程上的必然而非提示文案。
        person = await person_store.set_consent(
            db, owner_type, int(person_id), granted=True, subject=consent_subject,
            method=consent_method, operator_id=actor.id, note="建档时同步登记")
        enroll_row = await person_store.get_enrollment(db, owner_type, int(person_id), create=True)
        assert enroll_row is not None

    photo_path, avatar_path = _save_images(owner_type, int(person_id), img, aligned)
    enroll_row.photo_path = enroll_row.photo_path or photo_path
    enroll_row.avatar_path = avatar_path or enroll_row.avatar_path
    enroll_row.quality_score = float(report.score)
    enroll_row.enrolled_at = _utcnow()
    enroll_row.updated_at = enroll_row.enrolled_at

    db.add(FaceTemplate(enrollment_id=enroll_row.id, embedding=vec.astype(np.float32).tobytes(),
                        dim=int(vec.shape[0]), model_name="sface",
                        quality=float(report.score), source="register"))
    await db.commit()

    await face_index.rebuild(db)
    await person_store.audit(db, action="face.enroll", target_type=owner_type,
                             target_id=str(person_id),
                             detail=f"{person['name']} 质量={report.score} 主体={consent_subject}",
                             actor=actor)

    detail = await person_store.get_person(db, owner_type, int(person_id))
    return {"ok": True, "quality": report.as_dict(), "duplicate": duplicate,
            "template_count": len((detail or {}).get("templates") or []),
            "person": detail, "reasons": [], "detail": "建档成功"}


@router.delete("/{owner_type}/{person_id}")
async def delete_face(
    owner_type: str,
    person_id: int,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    """注销人脸：删除全部模板与图片，人员档案保留。"""
    if owner_type not in person_store.OWNER_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知人员类型")
    removed = await person_store.purge_face(db, owner_type, person_id)
    await db.commit()
    await face_index.rebuild(db)
    await person_store.audit(db, action="face.delete", target_type=owner_type,
                             target_id=str(person_id), detail=f"删除 {removed} 条模板", actor=actor)
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------- 以图找人
@router.post("/identify")
async def identify(
    request: Request,
    pick: str = Query("strict", pattern="^(strict|largest)$"),
    top_k: int = Query(5, ge=1, le=10),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    """上传一张图片 → 返回可能的人员信息（1:N 检索）。

    这是"根据图片检测学生信息"的直接落地，也用于**批量建档前的查重**：
    先识别再决定是新建还是并入已有的人。
    """
    img = await _read_image(file)
    faces = face_detector.detect(img)
    face, reason = _pick_face(faces, pick)
    if face is None:
        return {"ok": False, "reasons": [reason], "candidates": []}

    report = face_id.quality(img, face)
    aligned = face_id.align(img, np.asarray(face["raw"], dtype=np.float32))
    vec = face_id.feature_from_aligned(aligned) if aligned is not None else None
    if vec is None:
        return {"ok": False, "reasons": ["特征提取失败，请换一张正面清晰的照片"],
                "quality": report.as_dict(), "candidates": []}

    decision = face_index.identify(vec, threshold=settings.FACE_MATCH_THRESHOLD,
                                   margin=settings.FACE_MATCH_MARGIN)
    candidates = [{
        "score": round(float(c["score"]), 4),
        "owner_type": c["owner_type"],
        "person_id": c["owner_id"],
        **(_person_brief(c["summary"]) or {}),
    } for c in decision.get("candidates", [])[:top_k]]

    await person_store.audit(db, action="face.identify",
                             detail=f"匹配={decision.get('matched')} 原因={decision.get('reason')}",
                             actor=actor, ip=request.client.host if request.client else "")

    return {
        "ok": True,
        "matched": bool(decision.get("matched")),
        "reason": decision.get("reason", ""),
        "score": decision.get("score"),
        "margin": decision.get("margin"),
        "quality": report.as_dict(),
        "face_bbox": face["bbox"],
        "person": _person_brief(decision.get("summary")),
        "candidates": candidates,
    }
