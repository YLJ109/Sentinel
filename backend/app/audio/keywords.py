"""关键词检测：文本归一化 + 精确匹配 + 编辑距离模糊匹配（容忍 ASR 误字）。

离线 ASR 对同音字/近音字经常出错（"打死你" → "打士你"），
纯子串匹配会漏报。中文只有单字差异，用相似度比率衡量会明显偏低
（3 字词错 1 字，序列相似度仅 0.67），因此这里改用**归一化编辑距离**：
按关键词长度放允许的错字数（3 字容 1 字、6 字容 2 字），
并对插入/删除各扩一个字符的窗口，兼顾召回与误报。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.core.config import settings

# 去掉空白与常见标点（中英文）
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】…—\-~·]+")
_FUZZY_MIN_LEN = 3


@dataclass
class KeywordHit:
    keyword: str
    category: str        # bullying / alarm / custom
    score: float         # 1.0 = 精确命中
    start: int
    end: int
    distance: int = 0    # 模糊命中的编辑距离，0 表示精确


def normalize(text: str) -> str:
    """全角转半角、去标点空白、英文小写。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    return _PUNCT.sub("", text)


def build_dictionary(extra: list[str] | None = None) -> dict[str, str]:
    """构造 关键词 -> 类别 的字典（去掉归一化后为空的项）。"""
    table: dict[str, str] = {}
    for kw in settings.KEYWORD_BULLYING:
        nk = normalize(kw)
        if nk:
            table[nk] = "bullying"
    for kw in settings.KEYWORD_ALARM:
        nk = normalize(kw)
        if nk:
            table.setdefault(nk, "alarm")
    for kw in extra or []:
        nk = normalize(kw)
        if nk:
            table.setdefault(nk, "custom")
    return table


def allowed_distance(length: int) -> int:
    """按关键词长度决定可容忍的错字数。"""
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
    """在文本中找与关键词编辑距离最小且达标的窗口。"""
    base = len(kw)
    best: tuple[int, int, int] | None = None
    for size in (base, base + 1, base - 1):
        if size <= 0 or size > len(norm):
            continue
        for i in range(0, len(norm) - size + 1):
            d = levenshtein(norm[i:i + size], kw, allow)
            if d <= allow and (best is None or d < best[2]):
                best = (i, i + size, d)
                if d == 0:
                    return best
    return best


def scan(text: str, extra_keywords: list[str] | None = None, fuzzy: bool = True) -> list[KeywordHit]:
    """扫描文本中的关键词，返回按出现位置排序的命中列表。"""
    norm = normalize(text)
    if not norm:
        return []

    hits: list[KeywordHit] = []
    for kw, category in build_dictionary(extra_keywords).items():
        idx = norm.find(kw)
        if idx >= 0:
            hits.append(KeywordHit(kw, category, 1.0, idx, idx + len(kw), 0))
            continue

        allow = allowed_distance(len(kw))
        if not fuzzy or allow == 0:
            continue

        found = _fuzzy_find(norm, kw, allow)
        if found is None:
            continue
        start, end, dist = found
        hits.append(KeywordHit(kw, category, round(1 - dist / max(1, len(kw)), 3), start, end, dist))

    hits.sort(key=lambda h: h.start)
    return hits


def summarize(hits: list[KeywordHit]) -> dict[str, object]:
    """聚合命中结果：文本列表 + 分类 + 最高风险等级。"""
    if not hits:
        return {"keywords": [], "bullying": [], "alarm": [], "level": None, "score": 0.0}
    bullying = [h.keyword for h in hits if h.category == "bullying"]
    alarm = [h.keyword for h in hits if h.category in ("alarm", "custom")]
    level = "high" if bullying else ("medium" if alarm else "low")
    return {
        "keywords": [h.keyword for h in hits],
        "bullying": bullying,
        "alarm": alarm,
        "level": level,
        "score": max(h.score for h in hits),
    }
