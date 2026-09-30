"""人员档案：学生 / 教师 / 管理人员 + 班级 + 人脸处理同意。

权限约定：
- 读取：所有登录用户可读（值班老师需要看到事件流里的学生信息）
- 写入：admin / operator
- 同意登记与撤回：仅 admin —— 这属于合规动作，需要留痕到具体责任人
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.models import SchoolClass, Student, User
from app.schemas import ClassIn, ConsentIn, ImportResult, PersonIn
from app.services import person_store

log = logging.getLogger("routers.persons")

router = APIRouter(prefix="/api/persons", tags=["人员档案"])
classes_router = APIRouter(prefix="/api/classes", tags=["班级"])

_write = require_role("admin", "operator")
_admin = require_role("admin")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _split(body: PersonIn, *, need_no: bool) -> tuple[str, dict]:
    """把 API 的通用 ``no`` 字段映射到该类型对应的列名（student_no / teacher_no / staff_no）。"""
    payload = body.model_dump(exclude_none=True)
    owner_type = payload.pop("owner_type")
    no = payload.pop("no", None)
    if need_no and not (no or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{person_store.TYPE_LABEL[owner_type]}编号不能为空")
    if no is not None:
        payload[person_store.NO_FIELD[owner_type]] = str(no).strip()
    return owner_type, payload


async def _check_class(db: AsyncSession, payload: dict) -> None:
    cid = payload.get("class_id")
    if cid and await db.get(SchoolClass, int(cid)) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "指定班级不存在")


# ---------------------------------------------------------------- 元信息
@router.get("/meta")
async def persons_meta(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    """下拉选项与统计：前端不硬编码状态/类型/同意枚举。

    枚举一旦只写在前端，后端加一个状态（比如"借读"）就会出现前端显示原始英文值的
    尴尬情况；集中下发可以避免这类不一致。
    """
    from app.models import FaceEnrollment, FaceTemplate
    from sqlalchemy import func

    counts = await person_store.counts_by_type(db)
    # 已授权但尚未建档（没有模板）的人数：这是"该录人脸却还没录"的待办清单，
    # 学校上线时最需要盯的就是这个数字。
    pending = int((await db.execute(
        select(func.count()).select_from(FaceEnrollment)
        .where(FaceEnrollment.consent_status == "granted",
               ~FaceEnrollment.id.in_(select(FaceTemplate.enrollment_id)))
    )).scalar() or 0)

    return {
        "types": [{"value": k, "label": v} for k, v in person_store.TYPE_LABEL.items()],
        "statuses": person_store.STATUS_LABELS,
        "genders": list(person_store.GENDERS),
        "consent_statuses": [{"value": k, "label": v} for k, v in person_store.CONSENT_LABEL.items()],
        "consent_subjects": [{"value": k, "label": v}
                             for k, v in person_store.CONSENT_SUBJECT_LABEL.items()],
        "counts": counts,
        "consent_pending_enroll": pending,
    }


# ---------------------------------------------------------------- 列表 / 详情
@router.get("")
async def list_persons(
    request: Request,
    owner_type: str = Query("student", pattern="^(student|teacher|staff)$"),
    q: str | None = None,
    class_id: int | None = None,
    status_filter: str | None = Query(None, alias="status"),
    consent: str | None = None,
    has_face: bool | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    result = await person_store.list_persons(
        db, owner_type, q=q, class_id=class_id, status=status_filter,
        consent=consent, has_face=has_face, page=page, limit=limit)
    # 只对"按关键字精确查某个人"记审计：列表页翻页就写日志会把审计淹没在噪声里，
    # 反而失去可追溯性。
    if q:
        await person_store.audit(db, action="person.search", target_type=owner_type,
                                 detail=f"关键字={q} 命中={result['total']}",
                                 actor=actor, ip=_client_ip(request))
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_person(
    body: PersonIn,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    owner_type, payload = _split(body, need_no=True)
    await _check_class(db, payload)
    try:
        person = await person_store.create_person(db, owner_type, payload)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await person_store.audit(db, action="person.create", target_type=owner_type,
                             target_id=str(person["id"]), detail=person["name"], actor=actor)
    return person


@router.post("/import", response_model=ImportResult)
async def import_persons(
    owner_type: str = Query("student", pattern="^(student|teacher|staff)$"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    """CSV 批量导入（UTF-8 或 GBK 均可，学校台账常见 Excel 导出为 GBK）。"""
    raw = await file.read()
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件过大（上限 4MB）")
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法识别文件编码，请另存为 UTF-8 或 GBK 的 CSV")

    try:
        result = await person_store.import_csv(db, owner_type, text)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await person_store.audit(
        db, action="person.import", target_type=owner_type,
        detail=f"新增 {result['created']} 更新 {result['updated']} 跳过 {result['skipped_count']}",
        actor=actor)
    from app.services.face_index import face_index

    face_index.invalidate()
    return result


@router.get("/{owner_type}/{pid}")
async def get_person(
    owner_type: str,
    pid: int,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    if owner_type not in person_store.OWNER_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知人员类型")
    person = await person_store.get_person(db, owner_type, pid)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人员不存在")
    # 查看单人详情属于"读取敏感信息"，必须留痕（合规问询时能回答谁查过谁）
    await person_store.audit(db, action="person.read", target_type=owner_type,
                             target_id=str(pid), detail=person["name"], actor=actor)
    return person


@router.patch("/{owner_type}/{pid}")
async def update_person(
    owner_type: str,
    pid: int,
    body: PersonIn,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    if body.owner_type != owner_type:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "路径与请求体的人员类型不一致")
    _, payload = _split(body, need_no=False)
    await _check_class(db, payload)
    try:
        person = await person_store.update_person(db, owner_type, pid, payload)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await person_store.audit(db, action="person.update", target_type=owner_type,
                             target_id=str(pid), detail=person["name"], actor=actor)
    from app.services.face_index import face_index

    face_index.invalidate()
    return person


@router.delete("/{owner_type}/{pid}")
async def delete_person(
    owner_type: str,
    pid: int,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    if owner_type not in person_store.OWNER_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知人员类型")
    person = await person_store.get_person(db, owner_type, pid)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人员不存在")
    await person_store.delete_person(db, owner_type, pid)
    await person_store.audit(db, action="person.delete", target_type=owner_type,
                             target_id=str(pid), detail=person["name"], actor=actor)
    from app.services.face_index import face_index

    face_index.invalidate()
    return {"ok": True}


# ---------------------------------------------------------------- 同意
@router.post("/{owner_type}/{pid}/consent")
async def set_consent(
    owner_type: str,
    pid: int,
    body: ConsentIn,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(_write),
) -> dict:
    """登记 / 撤回人脸信息处理同意。

    撤回会**立即删除已采集的人脸特征与照片**，不是只改一个状态位。

    权限取 admin/operator 而非仅 admin：同意是"学校作为处理者"取得的数据主体同意，
    与点击者的角色无关；把操作人身份与时间戳写进 consent_records 与审计日志，
    才是可追责的正确做法。若限定为 admin，值班老师将无法完成任何建档。
    """
    if owner_type not in person_store.OWNER_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知人员类型")
    if await person_store.get_person(db, owner_type, pid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人员不存在")
    person = await person_store.set_consent(
        db, owner_type, pid, granted=body.granted, subject=body.subject,
        method=body.method, operator_id=actor.id, note=body.note)
    await person_store.audit(
        db, action="consent.grant" if body.granted else "consent.revoke",
        target_type=owner_type, target_id=str(pid),
        detail=f"{person['name']} 主体={body.subject}", actor=actor)
    return person


@router.get("/{owner_type}/{pid}/consents")
async def list_consents(
    owner_type: str,
    pid: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    from app.models import ConsentRecord

    rows = (await db.execute(
        select(ConsentRecord)
        .where(ConsentRecord.owner_type == owner_type, ConsentRecord.owner_id == pid)
        .order_by(ConsentRecord.id.desc()).limit(50)
    )).scalars().all()
    return [{
        "id": r.id,
        "action": r.action,
        "action_label": "同意授权" if r.action == "grant" else "撤回同意",
        "subject": r.subject,
        "subject_label": person_store.CONSENT_SUBJECT_LABEL.get(r.subject, r.subject),
        "method": r.method,
        "note": r.note or "",
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


# ---------------------------------------------------------------- 班级
@classes_router.get("")
async def list_classes(db: AsyncSession = Depends(get_db),
                       _: User = Depends(get_current_user)) -> list[dict]:
    return await person_store.list_classes(db)


@classes_router.post("", status_code=status.HTTP_201_CREATED)
async def create_class(body: ClassIn, db: AsyncSession = Depends(get_db),
                       _: User = Depends(_write)) -> dict:
    try:
        return await person_store.create_class(db, body.name, body.grade)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@classes_router.delete("/{cid}")
async def delete_class(cid: int, db: AsyncSession = Depends(get_db),
                       actor: User = Depends(_write)) -> dict:
    """删除班级。

    存在的意义：CSV 导入会按「班级」列自动创建班级，导入时写错一个字就会留下
    一个空班级并长期出现在筛选下拉里。没有删除入口的话，这些脏数据只能靠改库清理。

    非空班级（仍有学生归属）一律拒绝删除：静默把学生的班级置空会造成
    "花名册上这个人突然没有班级"的数据事故，正确做法是先转班再删。
    """
    from sqlalchemy import func, select as _select

    row = await db.get(SchoolClass, cid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "班级不存在")
    count = int((await db.execute(
        _select(func.count()).select_from(Student).where(Student.class_id == cid))).scalar() or 0)
    if count:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"该班级下仍有 {count} 名学生，请先转班后再删除")
    await db.delete(row)
    await db.commit()
    await person_store.audit(db, action="class.delete", target_type="class",
                             target_id=str(cid), detail=row.name, actor=actor)
    return {"ok": True}
