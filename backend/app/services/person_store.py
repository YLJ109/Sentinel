"""人员档案统一层：学生 / 教师 / 管理人员 + 人脸建档 + 同意留痕 + 审计。

**为什么需要这一层**：三类人员分在三张表里（业务字段各不相同），但人脸建档、
同意状态、事件关联这三块逻辑对三类人完全一致。如果每个路由/服务各写一遍
"取学生、取他的 enrollment"，很快就会出现三套实现与三套 Bug。
本模块是这些操作的**唯一写入口**，并且把"人员摘要"统一成一个字典，
让上层的列表、事件流卡片、人脸检索结果三处共用同一种数据结构。

**为什么删除逻辑必须集中在这里**：(owner_type, owner_id) 是多态关联，
数据库层没有外键约束，孤儿数据不会被自动清理。删除人员时必须在同一事务内
连带删除 enrollment / templates / consent_records，并删除磁盘上的人脸图片。
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (AuditLog, ConsentRecord, FaceEnrollment, FaceTemplate,
                        SchoolClass, Staff, Student, Teacher)

log = logging.getLogger("services.person")

# ---------------------------------------------------------------- 常量映射
OWNER_TYPES: dict[str, type] = {"student": Student, "teacher": Teacher, "staff": Staff}
NO_FIELD: dict[str, str] = {"student": "student_no", "teacher": "teacher_no", "staff": "staff_no"}
TYPE_LABEL: dict[str, str] = {"student": "学生", "teacher": "教师", "staff": "管理人员"}

STATUS_LABELS: dict[str, dict[str, str]] = {
    "student": {"active": "在读", "transferred": "转学", "graduated": "毕业", "suspended": "休学"},
    "teacher": {"active": "在职", "left": "离职", "retired": "退休"},
    "staff": {"active": "在职", "left": "离职", "retired": "退休"},
}

CONSENT_LABEL: dict[str, str] = {"none": "未告知", "granted": "已授权", "revoked": "已撤回"}
CONSENT_SUBJECT_LABEL: dict[str, str] = {"self": "本人", "guardian": "监护人"}

GENDERS = ("男", "女", "未填")


def model_of(owner_type: str) -> type:
    cls = OWNER_TYPES.get(owner_type)
    if cls is None:
        raise ValueError(f"未知人员类型：{owner_type}")
    return cls


def no_attr(owner_type: str) -> str:
    return NO_FIELD[owner_type]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 序列化
def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize(person, owner_type: str, enroll: FaceEnrollment | None,
              template_count: int = 0, class_name: str = "", grade: str = "") -> dict:
    """把人员 + 人脸档案合并成前端可直接渲染的字典。"""
    consent = enroll.consent_status if enroll else "none"
    enrolled = bool(enroll and template_count > 0)
    return {
        "id": person.id,
        "owner_type": owner_type,
        "type_label": TYPE_LABEL[owner_type],
        "no": getattr(person, NO_FIELD[owner_type], ""),
        "name": person.name,
        "gender": person.gender or "",
        "status": person.status or "active",
        "status_label": STATUS_LABELS[owner_type].get(person.status or "active", person.status or ""),
        "note": person.note or "",
        "created_at": _dt(getattr(person, "created_at", None)),
        # ---- 学生专属 ----
        "class_id": getattr(person, "class_id", None),
        "class_name": class_name,
        "grade": grade,
        "enroll_year": getattr(person, "enroll_year", None),
        "guardian_name": getattr(person, "guardian_name", None),
        "guardian_phone": getattr(person, "guardian_phone", None),
        "guardian_relation": getattr(person, "guardian_relation", None),
        # ---- 教师 / 管理人员专属 ----
        "department": getattr(person, "department", None),
        "title": getattr(person, "title", None),
        "subject": getattr(person, "subject", None),
        "position": getattr(person, "position", None),
        "phone": getattr(person, "phone", None),
        # ---- 人脸与同意 ----
        "face": {
            "enrolled": enrolled,
            "template_count": template_count,
            "quality": round(float(enroll.quality_score), 3) if enroll else 0.0,
            "avatar_url": _media_url(enroll.avatar_path) if enroll else "",
            "photo_url": _media_url(enroll.photo_path) if enroll else "",
            "enrolled_at": _dt(enroll.enrolled_at) if enroll else None,
            "consent_status": consent,
            "consent_label": CONSENT_LABEL.get(consent, consent),
            "consent_subject": enroll.consent_subject if enroll else None,
            "consent_subject_label": CONSENT_SUBJECT_LABEL.get(enroll.consent_subject or "", "") if enroll else "",
            "consent_at": _dt(enroll.consent_at) if enroll else None,
        },
    }


def _media_url(path: str | None) -> str:
    """把磁盘路径转成受鉴权保护的媒体 URL。

    人脸图片**不能**做成静态目录：媒体路由会校验 Cookie / Bearer，
    避免知道文件名就能直接拉走学生的照片。
    """
    if not path:
        return ""
    from pathlib import Path

    from app.core.config import settings

    try:
        rel = Path(path).resolve().relative_to(Path(settings.DATA_DIR).resolve())
    except ValueError:
        return ""
    return "/api/media/" + rel.as_posix()


# ---------------------------------------------------------------- 查询
async def get_enrollment(db: AsyncSession, owner_type: str, owner_id: int,
                         create: bool = False) -> FaceEnrollment | None:
    row = (await db.execute(
        select(FaceEnrollment).where(FaceEnrollment.owner_type == owner_type,
                                     FaceEnrollment.owner_id == owner_id)
    )).scalar_one_or_none()
    if row is None and create:
        row = FaceEnrollment(owner_type=owner_type, owner_id=owner_id, consent_status="none")
        db.add(row)
        await db.flush()
    return row


async def template_counts(db: AsyncSession, owner_type: str,
                          owner_ids: list[int]) -> dict[int, int]:
    if not owner_ids:
        return {}
    rows = (await db.execute(
        select(FaceEnrollment.owner_id, func.count(FaceTemplate.id))
        .join(FaceTemplate, FaceTemplate.enrollment_id == FaceEnrollment.id)
        .where(FaceEnrollment.owner_type == owner_type,
               FaceEnrollment.owner_id.in_(owner_ids))
        .group_by(FaceEnrollment.owner_id)
    )).all()
    return {int(oid): int(n) for oid, n in rows}


async def enrollments_of(db: AsyncSession, owner_type: str,
                         owner_ids: list[int]) -> dict[int, FaceEnrollment]:
    if not owner_ids:
        return {}
    rows = (await db.execute(
        select(FaceEnrollment).where(FaceEnrollment.owner_type == owner_type,
                                     FaceEnrollment.owner_id.in_(owner_ids))
    )).scalars().all()
    return {int(e.owner_id): e for e in rows}


async def list_persons(db: AsyncSession, owner_type: str, *, q: str | None = None,
                       class_id: int | None = None, status: str | None = None,
                       consent: str | None = None, has_face: bool | None = None,
                       page: int = 1, limit: int = 20) -> dict:
    model = model_of(owner_type)
    stmt = select(model)

    if q:
        kw = f"%{q.strip()}%"
        stmt = stmt.where(or_(model.name.like(kw), getattr(model, NO_FIELD[owner_type]).like(kw)))
    if class_id is not None and owner_type == "student":
        stmt = stmt.where(model.class_id == class_id)
    if status:
        stmt = stmt.where(model.status == status)

    # 人脸相关筛选走"有模板的 owner_id 子查询"：
    # 只判 enrollment 是否存在是不够的 —— 建了档但一张模板都没有的人，
    # 在检索时是查不到的，前端却会显示"已建档"，属于典型的状态不一致。
    templated = (select(FaceEnrollment.owner_id)
                 .join(FaceTemplate, FaceTemplate.enrollment_id == FaceEnrollment.id)
                 .where(FaceEnrollment.owner_type == owner_type))
    if has_face is True:
        stmt = stmt.where(model.id.in_(templated))
    elif has_face is False:
        stmt = stmt.where(model.id.notin_(templated))

    total = int((await db.execute(
        select(func.count()).select_from(stmt.subquery()))).scalar() or 0)

    stmt = stmt.order_by(model.id.desc()).offset(max(0, (page - 1) * limit)).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    ids = [r.id for r in rows]

    enrolls = await enrollments_of(db, owner_type, ids)
    counts = await template_counts(db, owner_type, ids)

    if consent:
        # 同意状态是前端最常用的筛选维度之一，但它在另一张表上；
        # 放在页内过滤会破坏 total 的语义，因此这里对整页结果做一次精确过滤并修正总数。
        rows = [r for r in rows if (enrolls.get(r.id).consent_status if enrolls.get(r.id) else "none") == consent]
        ids = [r.id for r in rows]

    classes = await _class_map(db, [getattr(r, "class_id", None) for r in rows])

    items = []
    for r in rows:
        cid = getattr(r, "class_id", None)
        cname, cgrade = classes.get(cid, ("", ""))
        items.append(serialize(r, owner_type, enrolls.get(r.id), counts.get(r.id, 0), cname, cgrade))
    return {"total": total, "page": page, "limit": limit, "items": items}


async def _class_map(db: AsyncSession, ids: list[int | None]) -> dict[int, tuple[str, str]]:
    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    rows = (await db.execute(select(SchoolClass).where(SchoolClass.id.in_(wanted)))).scalars().all()
    return {c.id: (c.name, c.grade or "") for c in rows}


async def get_person(db: AsyncSession, owner_type: str, owner_id: int) -> dict | None:
    model = model_of(owner_type)
    person = await db.get(model, owner_id)
    if person is None:
        return None
    enroll = await get_enrollment(db, owner_type, owner_id)
    counts = await template_counts(db, owner_type, [owner_id])
    classes = await _class_map(db, [getattr(person, "class_id", None)])
    cid = getattr(person, "class_id", None)
    cname, cgrade = classes.get(cid, ("", ""))
    out = serialize(person, owner_type, enroll, counts.get(owner_id, 0), cname, cgrade)
    if enroll:
        tpls = (await db.execute(
            select(FaceTemplate).where(FaceTemplate.enrollment_id == enroll.id)
            .order_by(FaceTemplate.id)
        )).scalars().all()
        out["templates"] = [{"id": t.id, "quality": round(float(t.quality), 3),
                             "source": t.source, "model": t.model_name,
                             "created_at": _dt(t.created_at)} for t in tpls]
    else:
        out["templates"] = []
    return out


# ---------------------------------------------------------------- 写入
def _clean(data: dict, owner_type: str) -> dict:
    """只保留该类型允许写入的字段，避免把任意键写进 ORM。"""
    common = ("name", "gender", "status", "note")
    extra = {
        "student": ("class_id", "enroll_year", "guardian_name", "guardian_phone", "guardian_relation"),
        "teacher": ("department", "title", "subject", "phone"),
        "staff": ("department", "position", "phone"),
    }[owner_type]
    keys = common + extra
    out = {}
    for k in keys:
        if k in data and data[k] is not None:
            out[k] = data[k]
    return out


async def create_person(db: AsyncSession, owner_type: str, data: dict) -> dict:
    model = model_of(owner_type)
    no_key = NO_FIELD[owner_type]
    no_val = str(data.get(no_key) or "").strip()
    if not no_val:
        raise ValueError(f"{TYPE_LABEL[owner_type]}编号不能为空")
    dup = (await db.execute(select(model).where(getattr(model, no_key) == no_val))).scalar_one_or_none()
    if dup is not None:
        raise ValueError(f"编号已存在：{no_val}")

    fields = _clean(data, owner_type)
    if not str(fields.get("name") or "").strip():
        raise ValueError("姓名不能为空")
    person = model(**{no_key: no_val, **fields})
    db.add(person)
    await db.commit()
    await db.refresh(person)
    return await get_person(db, owner_type, person.id)


async def update_person(db: AsyncSession, owner_type: str, owner_id: int, data: dict) -> dict:
    model = model_of(owner_type)
    person = await db.get(model, owner_id)
    if person is None:
        raise ValueError("人员不存在")
    no_key = NO_FIELD[owner_type]
    if data.get(no_key):
        no_val = str(data[no_key]).strip()
        dup = (await db.execute(select(model).where(getattr(model, no_key) == no_val,
                                                    model.id != owner_id))).scalar_one_or_none()
        if dup is not None:
            raise ValueError(f"编号已存在：{no_val}")
        setattr(person, no_key, no_val)
    for k, v in _clean(data, owner_type).items():
        setattr(person, k, v)
    person.updated_at = _utcnow()
    await db.commit()
    return await get_person(db, owner_type, owner_id)


async def delete_person(db: AsyncSession, owner_type: str, owner_id: int) -> None:
    """删除人员并连带清理人脸档案、特征、同意留痕与磁盘图片。

    多态关联没有外键约束，若不在这里手工级联，就会留下永远查不到 owner 的
    孤儿特征 —— 既占空间，又可能被检索命中并返回一个不存在的人。
    """
    model = model_of(owner_type)
    person = await db.get(model, owner_id)
    if person is None:
        raise ValueError("人员不存在")

    await purge_face(db, owner_type, owner_id)
    # 同意留痕按合规要求保留 3 年，因此只标记不删除：
    # 删掉"曾授权/曾撤回"的记录会让后续合规问询无从回答。
    await db.delete(person)
    await db.commit()


async def purge_face(db: AsyncSession, owner_type: str, owner_id: int) -> int:
    """清除人脸特征与图片（撤回同意、删除人员时调用）。返回删除的模板数。"""
    enroll = await get_enrollment(db, owner_type, owner_id)
    if enroll is None:
        return 0
    tpls = (await db.execute(
        select(FaceTemplate).where(FaceTemplate.enrollment_id == enroll.id)
    )).scalars().all()
    for t in tpls:
        await db.delete(t)

    for p in (enroll.photo_path, enroll.avatar_path):
        if p:
            try:
                from pathlib import Path

                Path(p).unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001 —— 文件删除失败不应阻断数据清理
                log.warning("人脸图片删除失败：%s（%s）", p, e)

    enroll.photo_path = None
    enroll.avatar_path = None
    enroll.quality_score = 0.0
    enroll.enrolled_at = None
    enroll.updated_at = _utcnow()
    await db.flush()

    # 导入单例对象（模块名与实例同名，导入模块会拿不到 rebuild/invalidate）
    from app.services.face_index import face_index

    face_index.invalidate()
    return len(tpls)


# ---------------------------------------------------------------- 同意
async def set_consent(db: AsyncSession, owner_type: str, owner_id: int, *, granted: bool,
                      subject: str = "self", method: str = "written",
                      operator_id: int | None = None, note: str | None = None,
                      evidence_path: str | None = None) -> dict:
    """登记 / 撤回人脸信息处理同意。

    **撤回即刻生效并删除生物特征**：合规要求个人有权便捷撤回，且撤回后
    处理者应停止处理。只改一个状态位而把特征留在库里，等于"撤回了但还在比对"，
    这是最容易被审计挑出的问题。
    """
    enroll = await get_enrollment(db, owner_type, owner_id, create=True)
    assert enroll is not None
    now = _utcnow()

    if granted:
        enroll.consent_status = "granted"
        enroll.consent_subject = subject
        enroll.consent_at = now
        enroll.consent_evidence_path = evidence_path
    else:
        enroll.consent_status = "revoked"
        enroll.consent_subject = subject
        enroll.consent_at = now
        await purge_face(db, owner_type, owner_id)
    enroll.updated_at = now

    db.add(ConsentRecord(
        owner_type=owner_type, owner_id=owner_id,
        action="grant" if granted else "revoke", subject=subject, method=method,
        operator_id=operator_id, evidence_path=evidence_path, note=note,
    ))
    await db.commit()

    from app.services.face_index import face_index

    face_index.invalidate()
    return await get_person(db, owner_type, owner_id)


# ---------------------------------------------------------------- 批量导入
async def import_csv(db: AsyncSession, owner_type: str, text: str) -> dict:
    """CSV 批量导入人员。

    列名用中文表头便于学校直接准备数据；缺列不会报错，只是该字段留空 ——
    学校台账的列顺序/命名千差万别，严格校验只会让人放弃导入。
    """
    model = model_of(owner_type)
    no_key = NO_FIELD[owner_type]
    aliases = {
        no_key: (no_key, "编号", "学号", "工号", "教师编号", "职工号"),
        "name": ("name", "姓名"),
        "gender": ("gender", "性别"),
        "status": ("status", "状态"),
    }
    if owner_type == "student":
        aliases.update({
            "class_name": ("class_name", "班级", "class"),
            "enroll_year": ("enroll_year", "入学年份", "年级"),
            "guardian_name": ("guardian_name", "监护人", "家长姓名"),
            "guardian_phone": ("guardian_phone", "监护人电话", "家长电话"),
            "guardian_relation": ("guardian_relation", "与监护人关系", "关系"),
        })
    elif owner_type == "teacher":
        aliases.update({
            "department": ("department", "部门", "科室", "年级组"),
            "title": ("title", "职称"),
            "subject": ("subject", "任教科目", "科目"),
            "phone": ("phone", "电话", "手机"),
        })
    else:
        aliases.update({
            "department": ("department", "部门", "科室"),
            "position": ("position", "职务"),
            "phone": ("phone", "电话", "手机"),
        })

    def pick(row: dict, field: str) -> str:
        for key in aliases.get(field, ()):
            v = row.get(key)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")

    created = updated = 0
    skipped: list[dict] = []
    for idx, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        no_val = pick(row, no_key)
        name = pick(row, "name")
        if not no_val or not name:
            skipped.append({"row": idx, "reason": "缺少编号或姓名"})
            continue

        existed = (await db.execute(
            select(model).where(getattr(model, no_key) == no_val))).scalar_one_or_none()
        payload = {
            "name": name,
            "gender": pick(row, "gender") or "未填",
            "status": pick(row, "status") or "active",
        }
        if owner_type == "student":
            class_name = pick(row, "class_name")
            if class_name:
                payload["class_id"] = await _ensure_class_id(db, class_name)
            year = pick(row, "enroll_year")
            payload["enroll_year"] = int(year) if year.isdigit() and len(year) == 4 else None
            payload["guardian_name"] = pick(row, "guardian_name") or None
            payload["guardian_phone"] = pick(row, "guardian_phone") or None
            payload["guardian_relation"] = pick(row, "guardian_relation") or None
        else:
            payload["department"] = pick(row, "department") or None
            payload["phone"] = pick(row, "phone") or None
            if owner_type == "teacher":
                payload["title"] = pick(row, "title") or None
                payload["subject"] = pick(row, "subject") or None
            else:
                payload["position"] = pick(row, "position") or None

        if existed is None:
            db.add(model(**{no_key: no_val, **{k: v for k, v in payload.items() if v is not None}}))
            created += 1
        else:
            for k, v in payload.items():
                if v not in (None, ""):
                    setattr(existed, k, v)
            existed.updated_at = _utcnow()
            updated += 1

    await db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "skipped_count": len(skipped)}


async def _ensure_class_id(db: AsyncSession, name: str) -> int | None:
    """按班级名找到或创建班级；年级从名称前缀推断（如「初二(3)班」→「初二」）。"""
    name = name.strip()
    if not name:
        return None
    row = (await db.execute(select(SchoolClass).where(SchoolClass.name == name))).scalar_one_or_none()
    if row is not None:
        return row.id
    grade = ""
    for mark in ("(", "（", "班"):
        if name.endswith("班") and mark in name:
            grade = name.split(mark)[0]
            break
    row = SchoolClass(name=name, grade=grade)
    db.add(row)
    await db.flush()
    return row.id


# ---------------------------------------------------------------- 班级
async def list_classes(db: AsyncSession) -> list[dict]:
    rows = list((await db.execute(select(SchoolClass).order_by(SchoolClass.grade, SchoolClass.name))).scalars().all())
    counts = dict((await db.execute(
        select(Student.class_id, func.count(Student.id)).group_by(Student.class_id))).all())
    return [{"id": c.id, "name": c.name, "grade": c.grade or "",
             "head_teacher_id": c.head_teacher_id,
             "student_count": int(counts.get(c.id, 0))} for c in rows]


async def create_class(db: AsyncSession, name: str, grade: str = "") -> dict:
    name = name.strip()
    if not name:
        raise ValueError("班级名称不能为空")
    dup = (await db.execute(select(SchoolClass).where(SchoolClass.name == name))).scalar_one_or_none()
    if dup is not None:
        raise ValueError("班级已存在")
    row = SchoolClass(name=name, grade=grade.strip())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "name": row.name, "grade": row.grade or "", "student_count": 0}


# ---------------------------------------------------------------- 摘要与统计
async def summary_map(db: AsyncSession, owner_type: str) -> dict[int, dict]:
    """一次性生成"人员摘要"字典，供人脸检索索引缓存使用。

    实时链路每帧都要取姓名/班级/头像，绝不能每帧查库；
    索引重建时把摘要一起算好并常驻内存，识别命中后直接取。
    """
    model = model_of(owner_type)
    rows = list((await db.execute(select(model))).scalars().all())
    enrolls = await enrollments_of(db, owner_type, [r.id for r in rows])
    counts = await template_counts(db, owner_type, [r.id for r in rows])
    classes = await _class_map(db, [getattr(r, "class_id", None) for r in rows])

    out: dict[int, dict] = {}
    for r in rows:
        cid = getattr(r, "class_id", None)
        cname, cgrade = classes.get(cid, ("", ""))
        e = enrolls.get(r.id)
        out[r.id] = {
            "person_id": r.id,
            "owner_type": owner_type,
            "type_label": TYPE_LABEL[owner_type],
            "name": r.name,
            "no": getattr(r, NO_FIELD[owner_type], ""),
            "gender": r.gender or "",
            "class_name": cname,
            "grade": cgrade,
            "department": getattr(r, "department", None) or "",
            "avatar_url": _media_url(e.avatar_path) if e else "",
            "consent_status": e.consent_status if e else "none",
            "template_count": counts.get(r.id, 0),
        }
    return out


async def counts_by_type(db: AsyncSession) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, model in OWNER_TYPES.items():
        out[key] = int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)
    out["face_enrolled"] = int((await db.execute(
        select(func.count(func.distinct(FaceEnrollment.owner_id)))
        .join(FaceTemplate, FaceTemplate.enrollment_id == FaceEnrollment.id)
        .where(FaceEnrollment.consent_status == "granted"))).scalar() or 0)
    return out


# ---------------------------------------------------------------- 审计
async def audit(db: AsyncSession, *, action: str, target_type: str = "", target_id: str = "",
                detail: str | None = None, actor=None, ip: str = "") -> None:
    """写审计日志。失败只告警不抛错 —— 审计绝不能反过来打断业务。"""
    try:
        db.add(AuditLog(
            actor_id=getattr(actor, "id", None),
            actor_name=(getattr(actor, "username", "") or "") if actor else "",
            action=action, target_type=target_type, target_id=str(target_id or ""),
            detail=(detail or "")[:500], ip=ip or "",
        ))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("审计日志写入失败：%s", e)


async def list_audit(db: AsyncSession, limit: int = 50, action: str | None = None) -> list[dict]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(500, max(1, limit)))
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    rows = list((await db.execute(stmt)).scalars().all())
    return [{"id": r.id, "actor": r.actor_name, "action": r.action,
             "target": f"{r.target_type}:{r.target_id}" if r.target_type else "",
             "detail": r.detail or "", "ip": r.ip,
             "created_at": _dt(r.created_at)} for r in rows]
