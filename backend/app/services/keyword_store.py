"""关键词词表的持久化与内存缓存。

**读写分离**是这个模块的核心设计：
    读路径（语音转写时的 ``scan``）走**纯内存缓存，永不查库**。
    语音回调运行在同步上下文，而项目用的是 async SQLAlchemy ——
    每次转写都去建连接会拖慢实时链路，也会让单机 SQLite 承受不必要的压力。
    写路径（CRUD 接口、首次灌库）负责落库，并在写完后刷新缓存。

    代价是"词表变更"与"生效"之间隔一次缓存刷新，但刷新由写接口主动触发，
    因此对使用者而言仍是即时生效。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KeywordRule

log = logging.getLogger("services.keyword")

# 分类 / 档位的合法取值
VALID_LEVELS = ("alarm", "warn", "highlight")
VALID_CATEGORIES = ("threat", "insult", "isolate", "extort", "intimidate",
                    "help", "violence", "cyber", "custom")

# 当前生效词表缓存：[(词, 分类, 档位), ...]
_cache: list[tuple[str, str, str]] = []


def load_active_rules() -> list[tuple[str, str, str]]:
    """同步返回当前生效词表（纯内存，供 scan 调用）。"""
    return _cache


async def refresh(db: AsyncSession) -> int:
    """从数据库重新加载启用中的词表到缓存，返回条数。"""
    global _cache
    rows = (await db.execute(
        select(KeywordRule.word, KeywordRule.category, KeywordRule.level)
        .where(KeywordRule.enabled.is_(True))
        .order_by(KeywordRule.id)
    )).all()
    _cache = [(w, c, lv) for w, c, lv in rows]
    # 词表变了，AC 自动机缓存必须失效，否则仍按旧词表匹配
    from app.audio import keywords as kw

    kw.invalidate()
    log.info("关键词词表已刷新：%d 条", len(_cache))
    return len(_cache)


async def ensure_seeded(db: AsyncSession) -> int:
    """库为空时灌入内置词库（约 3000 条）。

    只在空表时执行，避免每次启动都做一次全量比对；
    内置词标记 ``is_preset=True``，界面允许停用但不允许删除。
    """
    total = (await db.execute(select(func.count()).select_from(KeywordRule))).scalar() or 0
    if total:
        return 0

    from app.audio.lexicon import build_lexicon

    items = build_lexicon()
    db.add_all([
        KeywordRule(word=w, category=c, level=lv, enabled=True,
                    note=("内置词库（自动扩展）" if origin == "expanded" else "内置词库"),
                    is_preset=True)
        for w, c, lv, origin in items
    ])
    await db.commit()
    log.info("内置关键词词库已灌入：%d 条", len(items))
    return len(items)


# ---------------------------------------------------------------- 查询
async def list_rules(
    db: AsyncSession,
    keyword: str | None = None,
    level: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
    offset: int = 0,
    limit: int = 50,
    order: str = "hit",
) -> tuple[list[KeywordRule], int]:
    """分页查询词表。默认按命中次数倒序 —— 最常命中的词最值得关注。"""
    cond = []
    if keyword:
        cond.append(KeywordRule.word.contains(keyword.strip()))
    if level:
        cond.append(KeywordRule.level == level)
    if category:
        cond.append(KeywordRule.category == category)
    if enabled is not None:
        cond.append(KeywordRule.enabled.is_(enabled))

    base = select(KeywordRule)
    if cond:
        base = base.where(*cond)

    total = (await db.execute(
        select(func.count()).select_from(base.subquery()))).scalar() or 0

    order_by = {
        "hit": KeywordRule.hit_count.desc(),
        "word": KeywordRule.word.asc(),
        "new": KeywordRule.id.desc(),
    }.get(order, KeywordRule.hit_count.desc())

    rows = list((await db.execute(
        base.order_by(order_by).offset(offset).limit(limit))).scalars().all())
    return rows, int(total)


async def level_counts(db: AsyncSession) -> dict[str, int]:
    """按档位统计词表规模（含停用），供页面顶部的概览展示。"""
    rows = (await db.execute(
        select(KeywordRule.level, func.count())
        .group_by(KeywordRule.level))).all()
    out = {lv: 0 for lv in VALID_LEVELS}
    for lv, n in rows:
        out[lv] = int(n)
    out["total"] = sum(out[lv] for lv in VALID_LEVELS)
    return out


# ---------------------------------------------------------------- 写入
def _conflict(word: str) -> str:
    return f"关键词已存在：{word}"


async def _exists(db: AsyncSession, word: str, exclude_id: int | None = None) -> bool:
    q = select(func.count()).select_from(KeywordRule).where(KeywordRule.word == word)
    if exclude_id is not None:
        q = q.where(KeywordRule.id != exclude_id)
    return bool((await db.execute(q)).scalar())


async def create_rule(db: AsyncSession, word: str, category: str, level: str,
                      note: str | None = None, is_preset: bool = False) -> KeywordRule:
    rule = KeywordRule(word=word.strip(), category=category, level=level,
                       note=note, is_preset=is_preset, enabled=True)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def update_rule(db: AsyncSession, rule_id: int, **fields) -> KeywordRule | None:
    rule = await db.get(KeywordRule, rule_id)
    if rule is None:
        return None
    for key in ("word", "category", "level", "enabled", "note"):
        if key in fields and fields[key] is not None:
            setattr(rule, key, fields[key])
    rule.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rule)
    return rule


async def delete_rule(db: AsyncSession, rule_id: int) -> str | None:
    """删除词条。内置词拒绝删除并返回原因（应改为停用）。"""
    rule = await db.get(KeywordRule, rule_id)
    if rule is None:
        return "词条不存在"
    if rule.is_preset:
        return "内置词不支持删除，请改为「停用」"
    await db.delete(rule)
    await db.commit()
    return None


async def bulk_import(db: AsyncSession, text: str, category: str,
                      level: str) -> dict[str, object]:
    """批量导入：按行解析，自动去重与跳过非法项。

    返回逐行结果，让使用者能清楚知道哪一行被跳过、为什么 ——
    批量导入最怕"静默丢弃"，用户会以为都进去了。
    """
    lines = [ln.strip() for ln in (text or "").replace("、", "\n").replace(",", "\n").splitlines()]
    lines = [ln for ln in lines if ln]

    existing = {w for (w,) in (await db.execute(
        select(KeywordRule.word))).all()}

    added: list[str] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for ln in lines:
        if len(ln) > 32:
            skipped.append({"word": ln[:32], "reason": "过长（超过 32 字）"})
            continue
        if ln in existing or ln in seen:
            skipped.append({"word": ln, "reason": "已存在"})
            continue
        seen.add(ln)
        added.append(ln)

    if added:
        db.add_all([KeywordRule(word=w, category=category, level=level,
                                note="批量导入", is_preset=False) for w in added])
        await db.commit()

    return {"added": len(added), "skipped": len(skipped), "details": skipped[:50]}


async def bump_hits(db: AsyncSession, words: list[str]) -> None:
    """累加命中次数。

    命中是低频事件（一句话命中才有），因此直接落库，
    不必为它引入内存缓冲 + 定时刷新的复杂度。
    """
    uniq = [w for w in dict.fromkeys(words) if w]
    if not uniq:
        return
    await db.execute(
        update(KeywordRule)
        .where(KeywordRule.word.in_(uniq))
        .values(hit_count=KeywordRule.hit_count + 1))
    await db.commit()


async def reset_preset(db: AsyncSession) -> int:
    """把内置词恢复为「启用 + 原始档位」，用于误操作后的回滚。"""
    from app.audio.lexicon import build_lexicon

    preset = {w: (c, lv) for w, c, lv, _ in build_lexicon()}
    rows = list((await db.execute(
        select(KeywordRule).where(KeywordRule.is_preset.is_(True)))).scalars().all())
    changed = 0
    for rule in rows:
        meta = preset.get(rule.word)
        if meta and (not rule.enabled or rule.level != meta[1] or rule.category != meta[0]):
            rule.enabled = True
            rule.level = meta[1]
            rule.category = meta[0]
            changed += 1
    if changed:
        await db.commit()
    return changed


async def delete_non_preset(db: AsyncSession) -> int:
    """清空所有自定义词（内置词保留），用于词表重置。"""
    result = await db.execute(delete(KeywordRule).where(KeywordRule.is_preset.is_(False)))
    await db.commit()
    return int(result.rowcount or 0)


__all__ = [
    "VALID_LEVELS", "VALID_CATEGORIES",
    "load_active_rules", "refresh", "ensure_seeded", "list_rules", "level_counts",
    "create_rule", "update_rule", "delete_rule", "bulk_import", "bump_hits",
    "reset_preset", "delete_non_preset",
]
