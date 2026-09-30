"""关键词管理：词表的增删改查、批量导入与命中统计。

三档响应（level）：
    ``alarm``     命中即触发报警 —— 明确的伤害威胁与求救
    ``warn``      仅警告提示 —— 侮辱、孤立、勒索、恐吓
    ``highlight`` 仅高亮   —— 可疑肢体动作，作为人工复核线索

把词表扩到几千条后，最大的风险不是漏报而是误报泛滥（"垃圾""你妈"
在正常对话里也会出现），因此档位设计比词条数量更重要。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_role
from app.models import KeywordRule, User
from app.services import keyword_store as store

router = APIRouter(prefix="/api/keywords", tags=["关键词"])
log = logging.getLogger("routers.keywords")

# 读操作所有登录用户可用；写操作值班长及以上；删除与重置仅管理员
_write = require_role("admin", "operator")
_admin = require_role("admin")


class RuleIn(BaseModel):
    word: str = Field(..., min_length=1, max_length=32)
    category: str = "custom"
    level: str = "warn"
    note: str | None = Field(None, max_length=255)


class RulePatch(BaseModel):
    word: str | None = Field(None, min_length=1, max_length=32)
    category: str | None = None
    level: str | None = None
    enabled: bool | None = None
    note: str | None = Field(None, max_length=255)


class BulkIn(BaseModel):
    text: str = Field(..., description="按行或顿号/逗号分隔的词表")
    category: str = "custom"
    level: str = "warn"


def _to_dict(r: KeywordRule) -> dict:
    return {
        "id": r.id,
        "word": r.word,
        "category": r.category,
        "level": r.level,
        "enabled": bool(r.enabled),
        "note": r.note,
        "hit_count": int(r.hit_count or 0),
        "is_preset": bool(r.is_preset),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _check(level: str | None, category: str | None) -> None:
    if level is not None and level not in store.VALID_LEVELS:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST,
                            f"响应档位不合法，可选：{'/'.join(store.VALID_LEVELS)}")
    if category is not None and category not in store.VALID_CATEGORIES:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST,
                            f"分类不合法，可选：{'/'.join(store.VALID_CATEGORIES)}")


# 注意：具体路径（/meta、/stats）必须注册在 /{rule_id} 之前，
# 否则会被单段动态路由抢先匹配，int 转换失败直接返回 422。
@router.get("/meta")
async def keyword_meta(_: User = Depends(get_current_user)) -> dict:
    """页面初始化用：可选档位、分类、以及各档位词条数量。"""
    from app.audio import keywords as kw

    return {
        "levels": list(store.VALID_LEVELS),
        "categories": list(store.VALID_CATEGORIES),
        "level_labels": {"alarm": "报警", "warn": "警告", "highlight": "高亮"},
        "category_labels": {
            "threat": "威胁伤害", "insult": "侮辱贬损", "isolate": "孤立排挤",
            "extort": "勒索索要", "intimidate": "恐吓", "help": "求助呼救",
            "violence": "肢体冲突", "cyber": "网络霸凌", "custom": "自定义",
        },
        "engine": kw.stats(),
    }


@router.get("")
async def list_keywords(
    keyword: str | None = Query(None, description="按词条模糊搜索"),
    level: str | None = Query(None),
    category: str | None = Query(None),
    enabled: bool | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    order: str = Query("hit", description="hit 命中次数 / word 词条 / new 最新"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    rows, total = await store.list_rules(
        db, keyword=keyword, level=level, category=category,
        enabled=enabled, offset=offset, limit=limit, order=order)
    return {
        "items": [_to_dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "counts": await store.level_counts(db),
    }


@router.post("")
async def create_keyword(body: RuleIn, db: AsyncSession = Depends(get_db),
                         user: User = Depends(_write)) -> dict:
    word = body.word.strip()
    _check(body.level, body.category)
    if not word:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "关键词不能为空")
    if await store._exists(db, word):  # noqa: SLF001 —— 同模块内的轻量校验
        raise HTTPException(http_status.HTTP_409_CONFLICT, f"关键词已存在：{word}")
    rule = await store.create_rule(db, word, body.category, body.level, body.note)
    await store.refresh(db)
    log.info("新增关键词：%s（%s/%s）by %s", word, body.category, body.level, user.username)
    return _to_dict(rule)


@router.patch("/{rule_id}")
async def update_keyword(rule_id: int, body: RulePatch,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(_write)) -> dict:
    _check(body.level, body.category)
    if body.word:
        body.word = body.word.strip()
        if await store._exists(db, body.word, exclude_id=rule_id):
            raise HTTPException(http_status.HTTP_409_CONFLICT, f"关键词已存在：{body.word}")
    rule = await store.update_rule(db, rule_id, **body.model_dump(exclude_none=True))
    if rule is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "词条不存在")
    await store.refresh(db)
    log.info("修改关键词 #%s by %s", rule_id, user.username)
    return _to_dict(rule)


@router.delete("/{rule_id}")
async def delete_keyword(rule_id: int, db: AsyncSession = Depends(get_db),
                         user: User = Depends(_admin)) -> dict:
    reason = await store.delete_rule(db, rule_id)
    if reason:
        # 内置词拒绝删除属于业务约束，用 400 而非 404，前端可据此提示"请改为停用"
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, reason)
    await store.refresh(db)
    log.info("删除关键词 #%s by %s", rule_id, user.username)
    return {"ok": True}


@router.post("/bulk")
async def bulk_import(body: BulkIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(_write)) -> dict:
    """批量导入。返回逐行结果，被跳过的词会给出原因。"""
    _check(body.level, body.category)
    result = await store.bulk_import(db, body.text, body.category, body.level)
    await store.refresh(db)
    log.info("批量导入关键词：新增 %s 条，跳过 %s 条 by %s",
             result["added"], result["skipped"], user.username)
    return result


@router.post("/reset-preset")
async def reset_preset(db: AsyncSession = Depends(get_db),
                       user: User = Depends(_admin)) -> dict:
    """把内置词恢复为「启用 + 原始档位」，用于误操作后的回滚。"""
    changed = await store.reset_preset(db)
    await store.refresh(db)
    log.info("恢复内置词表：%s 条被还原 by %s", changed, user.username)
    return {"changed": changed}


@router.post("/clear-custom")
async def clear_custom(db: AsyncSession = Depends(get_db),
                       user: User = Depends(_admin)) -> dict:
    """清空所有自定义词（内置词保留）。"""
    removed = await store.delete_non_preset(db)
    await store.refresh(db)
    log.info("清空自定义关键词：%s 条 by %s", removed, user.username)
    return {"removed": removed}
