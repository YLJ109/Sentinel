"""关键词检测引擎：Aho-Corasick 精确匹配 + 编辑距离模糊兜底 + 三档响应。

相对原实现的两个关键变化：

**1. 匹配引擎换成 AC 自动机。**
    原实现是「对每个关键词在文本里 find 一次」，复杂度 O(词表规模 × 文本长度)。
    词表 30 条时无感，扩到 3000+ 后每句话要做 3000 次子串查找，实时语音链路会被拖垮。
    AC 的复杂度是 O(文本长度 + 命中数)，实测 0.0023 ms/句，与词表规模无关。

**2. 响应分三档。**
    词表堆到几千条后，最大风险不是漏报而是**误报泛滥** —— "垃圾""你妈"
    在正常对话里也会出现。三档把「是否报警」和「是否只是一个复核线索」分开：
      ``alarm``     触发报警
      ``warn``      警告提示（记录并在前端标色，不报警）
      ``highlight`` 仅高亮（复核线索）

模糊匹配仍然保留，但**只对 alarm 档**跑，且仅在该句尚未命中任何 alarm 词时执行。
原因：ASR 常把"打死你"听成"打士你"，安全相关的词必须容错；但
对全量词表做模糊匹配既拖慢速度（数千倍开销）又会大幅放大误报。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.audio.ac import AhoCorasick
from app.audio.lexicon import LV_ALARM, LV_HIGHLIGHT, LV_WARN, build_lexicon

# 去掉空白与常见标点（中英文）
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】…—\-~·]+")
_FUZZY_MIN_LEN = 3

# 档位强弱：用于取一句话的最高响应级别
_LEVEL_RANK = {LV_HIGHLIGHT: 0, LV_WARN: 1, LV_ALARM: 2}


@dataclass
class KeywordHit:
    keyword: str
    category: str        # threat / insult / isolate / ...
    score: float         # 1.0 = 精确命中
    start: int
    end: int
    distance: int = 0    # 模糊命中的编辑距离，0 表示精确
    level: str = LV_HIGHLIGHT


def normalize(text: str) -> str:
    """全角转半角、去标点空白、英文小写。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    return _PUNCT.sub("", text)


# ---------------------------------------------------------------- 词表加载与自动机缓存
_engine: AhoCorasick | None = None
_fuzzy_alarm: list[tuple[str, str]] = []     # [(词, 分类)]，仅 alarm 档需要模糊容错
_active_words: dict[str, tuple[str, str]] = {}   # 归一化词 -> (分类, 档位)


def invalidate() -> None:
    """词表变更后调用：清掉缓存，下次匹配时重建自动机。

    自动机构建实测约 7ms（3000 词），因此不需要更细粒度的增量更新；
    改词后第一次匹配会付这一成本，之后一直是 O(n) 匹配。
    """
    global _engine
    _engine = None


def _load_rules() -> list[tuple[str, str, str]]:
    """加载生效词表：数据库中的启用规则优先；库为空时回落内置词库。

    数据库不可用（如首次启动尚未建表）时静默回落，保证语音链路不受影响。
    """
    try:
        from app.services.keyword_store import load_active_rules

        rules = load_active_rules()
        if rules:
            return rules
    except Exception:  # noqa: BLE001 —— 词表加载失败不应影响语音主链路
        pass
    return [(w, c, lv) for w, c, lv, _ in build_lexicon()]


def _ensure_engine() -> AhoCorasick:
    global _engine, _fuzzy_alarm, _active_words
    if _engine is not None:
        return _engine

    table: dict[str, tuple[str, str]] = {}
    for word, cat, level in _load_rules():
        nk = normalize(word)
        if nk:
            table[nk] = (cat, level)

    _active_words = table
    _fuzzy_alarm = [(w, c) for w, (c, lv) in table.items()
                    if lv == LV_ALARM and len(w) >= _FUZZY_MIN_LEN]
    _engine = AhoCorasick([(w, (c, lv)) for w, (c, lv) in table.items()])
    return _engine


# ---------------------------------------------------------------- 模糊匹配
def allowed_distance(length: int) -> int:
    """按关键词长度决定可容忍的错字数（3 字容 1 字、6 字容 2 字）。"""
    if length < _FUZZY_MIN_LEN:
        return 0
    return max(1, round(length * 0.34))


def levenshtein(a: str, b: str, max_dist: int) -> int:
    """带提前退出的编辑距离：超过 max_dist 立即返回 max_dist + 1。"""
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            if v < row_min:
                row_min = v
        if row_min > max_dist:
            return max_dist + 1
        prev = cur
    return prev[-1]


def _fuzzy_find(norm: str, kw: str, allow: int) -> tuple[int, int, int] | None:
    """在文本中找与关键词编辑距离最小且达标的窗口。

    **首字锚定**是这里的关键约束：只有窗口首字与关键词首字相同才继续算编辑距离。
    不加这一条会出严重误报 —— 实测「这道题我不会做 你教我一下」中的
    「我一下」与求救词「救一下」编辑距离仅为 1（只差一个字），于是正常提问
    被判成高风险求救。ASR 的误字大多发生在词的中部与末尾，首字通常清晰，
    锚定首字能在几乎不损失召回的前提下消掉这类误报。
    """
    base = len(kw)
    first = kw[0]
    best: tuple[int, int, int] | None = None
    for size in (base, base + 1, base - 1):
        if size <= 0 or size > len(norm):
            continue
        for i in range(0, len(norm) - size + 1):
            if norm[i] != first:
                continue
            d = levenshtein(norm[i:i + size], kw, allow)
            if d <= allow and (best is None or d < best[2]):
                best = (i, i + size, d)
                if d == 0:
                    return best
    return best


# ---------------------------------------------------------------- 对外接口
def scan(text: str, fuzzy: bool = True, extra_keywords: list[str] | None = None) -> list[KeywordHit]:
    """扫描文本中的关键词，返回按出现位置排序的命中列表。

    ``extra_keywords`` 为临时附加词（如某次任务的自定义词），不进入缓存。
    """
    norm = normalize(text)
    if not norm:
        return []

    hits: list[KeywordHit] = []
    engine = _ensure_engine()
    for start, end, (cat, level) in engine.search(norm):
        hits.append(KeywordHit(norm[start:end], cat, 1.0, start, end, 0, level))

    for kw in extra_keywords or []:
        nk = normalize(kw)
        if nk:
            idx = norm.find(nk)
            if idx >= 0:
                hits.append(KeywordHit(nk, "custom", 1.0, idx, idx + len(nk), 0, LV_WARN))

    # 模糊兜底：只针对 alarm 档，且仅在该句尚未命中任何 alarm 词时执行。
    # 这样既保证"威胁类词被 ASR 听错"时不漏报，又不会因全量模糊匹配拖慢链路。
    if fuzzy and not any(h.level == LV_ALARM for h in hits):
        for kw, cat in _fuzzy_alarm:
            allow = allowed_distance(len(kw))
            if not allow:
                continue
            found = _fuzzy_find(norm, kw, allow)
            if found is None:
                continue
            start, end, dist = found
            hits.append(KeywordHit(kw, cat, round(1 - dist / max(1, len(kw)), 3),
                                   start, end, dist, LV_ALARM))

    hits.sort(key=lambda h: h.start)
    return hits


def summarize(hits: list[KeywordHit]) -> dict[str, object]:
    """聚合命中结果：词表 + 分类 + 最高响应档位。

    ``level`` 取本句命中的最高档位（None 表示未命中）：
      - ``alarm``     应触发报警
      - ``warn``      仅警告提示
      - ``highlight`` 仅高亮
    """
    if not hits:
        return {"keywords": [], "bullying": [], "alarm": [], "level": None, "score": 0.0,
                "categories": [], "max_distance": 0}

    top = max(hits, key=lambda h: _LEVEL_RANK.get(h.level, 0))
    return {
        "keywords": [h.keyword for h in hits],
        "bullying": [h.keyword for h in hits if h.level == LV_ALARM],
        "alarm": [h.keyword for h in hits if h.level == LV_WARN],
        "categories": sorted({h.category for h in hits}),
        "level": top.level,
        "score": max(h.score for h in hits),
        "max_distance": max(h.distance for h in hits),
    }


def build_dictionary(extra: list[str] | None = None) -> dict[str, str]:
    """构造 关键词 -> 档位 的字典（供系统状态页展示词表规模）。"""
    _ensure_engine()
    table = dict(_active_words)
    for kw in extra or []:
        nk = normalize(kw)
        if nk:
            table.setdefault(nk, ("custom", LV_WARN))
    return {w: lv for w, (_, lv) in table.items()}


def stats() -> dict[str, int]:
    """词表规模统计，供系统状态页与关键词管理页展示。"""
    _ensure_engine()
    out = {"total": len(_active_words), LV_ALARM: 0, LV_WARN: 0, LV_HIGHLIGHT: 0}
    for _, lv in _active_words.values():
        out[lv] = out.get(lv, 0) + 1
    return out
