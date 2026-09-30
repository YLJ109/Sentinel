"""Aho-Corasick 多模式串匹配自动机（纯 Python 实现，零新增依赖）。

为什么必须换掉原实现：
    原来的 ``scan()`` 是「对每个关键词在文本里 find 一次」，复杂度为
    O(词表规模 × 文本长度)。词表只有 30 条时看不出问题，但扩到几千条后，
    每一句语音转写都要做几千次子串查找 —— 实时链路会直接卡死。
    AC 自动机的匹配复杂度是 **O(文本长度 + 命中数)**，与词表规模无关，
    构建成本也只在词表变更时付一次。

适用场景：中文关键词大多是 2~6 字的子串，直接做子串匹配即可，
不需要分词（分词反而会切碎"打死你"这类短语）。
"""
from __future__ import annotations

from typing import Any, Iterable


class AhoCorasick:
    """基于 Trie + 失败指针的多模式匹配。

    构造后调用 :meth:`search` 返回全部命中（含重叠命中），
    每个命中携带注册时绑定的元数据（这里用来带分类与响应档位）。
    """

    __slots__ = ("_goto", "_fail", "_out", "_built", "_size")

    def __init__(self, patterns: Iterable[tuple[str, Any]]) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._out: list[list[Any]] = [[]]
        self._built = False
        self._size = 0

        for word, meta in patterns:
            if word:
                self._add(word, meta)
        # 词表为空时不构建失败指针，search 直接返回空
        if self._size:
            self._build()

    # ---------- 构建 ----------
    def _add(self, word: str, meta: Any) -> None:
        node = 0
        for ch in word:
            nxt = self._goto[node].get(ch)
            if nxt is None:
                self._goto.append({})
                self._fail.append(0)
                self._out.append([])
                nxt = len(self._goto) - 1
                self._goto[node][ch] = nxt
            node = nxt
        self._out[node].append((word, meta))
        self._size += 1

    def _build(self) -> None:
        """BFS 构造失败指针，并把输出集沿失败链合并（这样无需在匹配时回溯）。"""
        queue: list[int] = []
        for child in self._goto[0].values():
            self._fail[child] = 0
            queue.append(child)

        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for ch, child in self._goto[node].items():
                # 沿失败链找到第一个有该字符转移的祖先
                f = self._fail[node]
                while f and ch not in self._goto[f]:
                    f = self._fail[f]
                self._fail[child] = self._goto[f].get(ch, 0)
                # 合并输出：命中 child 时也命中其失败链上的所有词
                if self._fail[child] != child:
                    self._out[child].extend(self._out[self._fail[child]])
                queue.append(child)
        self._built = True

    # ---------- 匹配 ----------
    def search(self, text: str) -> list[tuple[int, int, Any]]:
        """返回 ``[(start, end, meta), ...]``，按结束位置自然有序。

        含重叠命中：例如词表中同时有"打你"与"打死你"，文本"打死你"会两条都命中，
        由调用方决定是保留最长匹配还是全部保留。
        """
        if not text or not self._size:
            return []

        out: list[tuple[int, int, Any]] = []
        node = 0
        goto, fail, emit = self._goto, self._fail, self._out
        for i, ch in enumerate(text):
            while node and ch not in goto[node]:
                node = fail[node]
            node = goto[node].get(ch, 0)
            if node:
                for word, meta in emit[node]:
                    out.append((i - len(word) + 1, i + 1, meta))
        return out

    def __len__(self) -> int:
        return self._size
