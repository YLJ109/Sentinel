"""合规：个人信息保护影响评估（PIA）、审计查询、告知同意书模板、合规自查看板。

本模块把《人脸识别技术应用安全管理办》里"学校必须做到"的几件事做成可操作的界面：

| 条款 | 要求 | 对应实现 |
|---|---|---|
| 第五条 / 第六条 | 显著方式告知 + 取得单独同意 | ``/notice`` 告知同意书模板 + 建档时登记同意 |
| 第七条 | 不满 14 岁需监护人同意 | ``consent_subject=guardian`` + 监护人字段 |
| 第八条 | 人脸信息存储于设备内、不得外传 | 全部落本机 data/faces，无任何外部上传 |
| 第八条 | 保存期限最短 | 撤回同意即删除特征与照片 |
| 第九条 | 事前 PIA + 处理记录，报告保存 ≥3 年 | ``/pia`` 台账 + ``/audit`` 审计 |
| 第十条 | 不得作为唯一验证方式 | 人员档案支持手工录入与 CSV 导入 |
| 第十三条 | 公共场所显著提示标识 | 自查看板里的"告知标识"检查项 |
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.models import FaceEnrollment, FaceTemplate, PiaRecord, User
from app.schemas import PiaIn
from app.services import person_store

log = logging.getLogger("routers.compliance")

router = APIRouter(prefix="/api/compliance", tags=["合规与隐私"])

_write = require_role("admin")


NOTICE_TEMPLATE = """\
《人脸信息处理告知同意书》

本单位（学校名称：________，联系方式：________）拟应用人脸识别技术处理您/您子女的
人脸信息，用于校园安全监测与欺凌事件处置辅助。为保障您的知情权与选择权，特此告知：

一、处理目的
    在校园公共区域（走廊、操场、食堂等）对异常行为（跌倒、推搡、打架等）进行
    自动识别与报警，帮助值班教师更快发现并处置校园欺凌事件。

二、处理方式与范围
    1. 仅在已张贴告知标识的公共区域采集，不涉及教室内部、卫生间、更衣室等私密场所；
    2. 人脸信息仅用于本单位内部的身份比对，不用于任何商业目的；
    3. 人脸信息与人脸特征**仅存储在本单位部署的服务器设备内，不通过互联网对外传输**；
    4. 被识别到的姓名、班级等档案信息仅向具备权限的值班教师与管理人员展示。

三、保存期限
    人脸特征与注册照片自建档之日起保存，至同意被撤回或人员离校为止；
    撤回同意后，本单位将在 1 个工作日内删除全部人脸特征与照片。

四、您的权利
    1. 您有权随时撤回本同意，撤回不影响撤回前已进行的处理的效力；
    2. 撤回方式：________（如：填写《人脸信息删除申请表》提交至德育处）；
    3. 您有权查询、复制本单位持有的您的相关信息，并要求更正错误信息。

五、拒绝的后果
    人脸识别**不是**本校安全管理的唯一方式。若您不同意，本单位将为您提供
    其他替代方式（人工巡查、刷卡门禁等），不影响您在校园内的任何权益。

本人已阅读并理解上述内容，自愿作出如下选择：

    □ 同意处理人脸信息
    □ 不同意处理人脸信息（将采用其他替代方式）

信息主体（签名）：________        与本人关系：□ 本人 □ 父 □ 母 □ 其他监护人
身份信息：________              日期：______年____月____日

特别提示：若信息主体不满十四周岁，须由父母或者其他监护人签署本同意书。
"""


@router.get("/notice")
async def notice(_: User = Depends(get_current_user)) -> dict:
    """返回告知同意书模板，供学校打印、张贴与存档。"""
    return {"title": "人脸信息处理告知同意书", "content": NOTICE_TEMPLATE,
            "note": "可复制到 Word 中调整排版后打印使用；签署原件请扫描归档，"
                    "并在人员档案的「同意状态」中登记。"}


@router.get("/pia")
async def list_pia(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    rows = list((await db.execute(
        select(PiaRecord).order_by(PiaRecord.id.desc()).limit(50))).scalars().all())
    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        expires = r.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        items.append({
            "id": r.id, "version": r.version, "scope": r.scope,
            "conclusion": r.conclusion or "", "risks": r.risks or "",
            "measures": r.measures or "", "reviewer": r.reviewer or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "expires_at": expires.isoformat() if expires else None,
            "expired": bool(expires and expires < now),
            "days_left": int((expires - now).days) if expires else None,
        })
    return {"items": items, "total": len(items)}


@router.post("/pia", status_code=status.HTTP_201_CREATED)
async def create_pia(body: PiaIn, db: AsyncSession = Depends(get_db),
                     actor: User = Depends(_write)) -> dict:
    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "到期时间格式不正确") from e
    else:
        # 默认一年一评：第九条要求目的/方式变更或重大安全事件时重新评估，
        # 定期复评是保证这条不落空的最低要求。
        expires = datetime.now(timezone.utc) + timedelta(days=365)

    row = PiaRecord(version=body.version, scope=body.scope, conclusion=body.conclusion,
                    risks=body.risks, measures=body.measures, reviewer=body.reviewer,
                    expires_at=expires)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await person_store.audit(db, action="pia.create", detail=f"版本 {body.version}", actor=actor)
    return {"id": row.id, "version": row.version}


@router.get("/audit")
async def list_audit(
    limit: int = Query(50, ge=1, le=500),
    action: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_write),
) -> dict:
    """敏感操作审计（仅管理员）：谁在什么时候查过/改过哪些人员信息。"""
    items = await person_store.list_audit(db, limit=limit, action=action)
    return {"items": items, "total": len(items)}


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db),
                   _: User = Depends(get_current_user)) -> dict:
    """合规自查看板：把"该做的事做了没有"变成一眼可读的数字。

    这也是应对外部问询最省力的方式 —— 不需要现场翻制度文件，
    直接把同意覆盖率、待建档人数、PIA 复评时间摆出来。
    """
    from sqlalchemy import func

    counts = await person_store.counts_by_type(db)
    total_people = counts.get("student", 0) + counts.get("teacher", 0) + counts.get("staff", 0)

    granted = int((await db.execute(
        select(func.count()).select_from(FaceEnrollment)
        .where(FaceEnrollment.consent_status == "granted"))).scalar() or 0)
    revoked = int((await db.execute(
        select(func.count()).select_from(FaceEnrollment)
        .where(FaceEnrollment.consent_status == "revoked"))).scalar() or 0)
    none_yet = int((await db.execute(
        select(func.count()).select_from(FaceEnrollment)
        .where(FaceEnrollment.consent_status == "none"))).scalar() or 0)

    last_pia = (await db.execute(
        select(PiaRecord).order_by(PiaRecord.id.desc()).limit(1))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    pia_expires = None
    if last_pia and last_pia.expires_at is not None:
        pia_expires = last_pia.expires_at
        if pia_expires.tzinfo is None:
            pia_expires = pia_expires.replace(tzinfo=timezone.utc)

    templates = int((await db.execute(
        select(func.count()).select_from(FaceTemplate))).scalar() or 0)

    return {
        "people": {"total": total_people, **{k: counts.get(k, 0) for k in ("student", "teacher", "staff")}},
        "consent": {
            "granted": granted,
            "revoked": revoked,
            "none": max(0, none_yet),
            # 未建档的（既没同意也没拒绝）人数，是学校推广阶段要处理的重点
            "coverage": round(granted / total_people, 4) if total_people else 0.0,
        },
        "face": {"templates": templates},
        "pia": {
            "has_record": last_pia is not None,
            "version": last_pia.version if last_pia else "",
            "expires_at": pia_expires.isoformat() if pia_expires else None,
            "expired": bool(pia_expires and pia_expires < now),
            "days_left": int((pia_expires - now).days) if pia_expires else None,
        },
        # 合规检查清单：前端逐条展示现状与做法，便于自查与对外说明
        "checklist": [
            {"key": "consent", "label": "人脸信息处理已取得单独同意",
             "done": granted > 0,
             "detail": f"已授权 {granted} 人 / 共 {total_people} 人"},
            {"key": "guardian", "label": "不满 14 周岁已取得监护人同意",
             "done": None,
             "detail": "请在学生档案中核对「同意主体」是否为监护人"},
            {"key": "local_only", "label": "人脸信息仅存储于本地设备，不对外传输",
             "done": True, "detail": "特征与注册照均落本机 data/faces，无任何外部上传接口"},
            {"key": "revoke", "label": "提供便捷的撤回同意方式",
             "done": True, "detail": "人员档案 →「撤回同意」会立即删除特征与照片"},
            {"key": "pia", "label": "已完成个人信息保护影响评估（至少每年复评）",
             "done": bool(last_pia and not (pia_expires and pia_expires < now)),
             "detail": (f"最近评估：{last_pia.version}｜"
                        f"{'已过期，请复评' if pia_expires and pia_expires < now else '有效'}"
                        ) if last_pia else "尚未开展评估"},
            {"key": "not_only", "label": "未将人脸识别作为唯一验证方式",
             "done": True, "detail": "人员档案支持手工录入与 CSV 批量导入，不依赖人脸"},
            {"key": "sign", "label": "采集区域已设置显著提示标识",
             "done": None, "detail": "请在安装了摄像头的区域张贴告知标识并拍照存档"},
            {"key": "retention", "label": "保存期限不超过实现目的所必需的最短时间",
             "done": True, "detail": "撤回同意或人员删除时同步清除特征与照片"},
        ],
    }
