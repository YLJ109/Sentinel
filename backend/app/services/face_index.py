"""人脸特征内存索引：读写分离，检索与词表规模无关。

**为什么用内存矩阵而不是向量数据库**：校园场景的底库规模在千级
（一所中学约 2000~4000 名师生）。这个量级下，把全部 128 维特征拼成一个
`(N, 128)` 的 float32 矩阵，1:N 检索就是一次矩阵乘法 —— 5000 人约 0.3ms，
比 FAISS 建索引更快、零维护成本、也少一个依赖。真正需要向量库是十万级以上。

**读写分离**（与关键词引擎同一套思路）：
- 读路径：引擎线程直接读内存矩阵，永不查库。逐帧识别如果每帧查一次库，
  5ms 的数据库往返会直接把实时链路拖垮。
- 写路径：注册 / 撤回 / 删除后由调用方显式 ``await rebuild(db)`` 重建。

**失败关闭（fail-closed）**：索引被标记为脏（刚发生过增删、重建尚未执行）时，
``identify`` 一律返回"未匹配"而不是拿旧数据去匹配。宁可暂时识别不出人，
也不能在被删除/已撤回同意的人身上还顶着姓名显示 —— 后者是合规事故，
前者只是功能短暂降级。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("services.face_index")

DIM = 128


@dataclass
class IndexEntry:
    enrollment_id: int
    owner_type: str
    owner_id: int
    summary: dict
    quality: float


class FaceIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix: np.ndarray | None = None
        self._entries: list[IndexEntry] = []
        self._dirty = True
        self._version = 0
        self._last_built_at = 0.0

    # ---------- 写路径 ----------
    def invalidate(self) -> None:
        """标记索引过期。任何人员/人脸/同意的变更后都必须调用。"""
        self._dirty = True

    async def rebuild(self, db) -> int:
        """从数据库全量重建索引。返回模板条数。"""
        # 延迟导入，避免 services 包内循环依赖
        from sqlalchemy import select

        from app.models import FaceEnrollment, FaceTemplate
        from app.services import person_store

        rows = (await db.execute(
            select(FaceTemplate, FaceEnrollment)
            .join(FaceEnrollment, FaceTemplate.enrollment_id == FaceEnrollment.id)
            # 只索引"已授权"的人：未授权者的特征不进内存，识别时无从命中
            .where(FaceEnrollment.consent_status == "granted")
        )).all()

        summaries: dict[str, dict[int, dict]] = {}
        for owner_type in ("student", "teacher", "staff"):
            summaries[owner_type] = await person_store.summary_map(db, owner_type)

        vecs: list[np.ndarray] = []
        entries: list[IndexEntry] = []
        for tpl, enroll in rows:
            summary = summaries.get(enroll.owner_type, {}).get(int(enroll.owner_id))
            if not summary:
                continue          # 人员已被删除但特征残留：跳过（孤儿数据）
            vec = np.frombuffer(tpl.embedding, dtype=np.float32)
            if vec.size != DIM:
                log.warning("特征维度异常，已跳过：enrollment=%s dim=%s", enroll.id, vec.size)
                continue
            norm = float(np.linalg.norm(vec))
            if norm < 1e-6:
                continue
            vecs.append(vec / norm)
            entries.append(IndexEntry(enrollment_id=int(enroll.id),
                                      owner_type=enroll.owner_type,
                                      owner_id=int(enroll.owner_id),
                                      summary=summary,
                                      quality=float(tpl.quality or 0.0)))

        matrix = np.vstack(vecs).astype(np.float32) if vecs else None
        with self._lock:
            self._matrix = matrix
            self._entries = entries
            self._dirty = False
            self._version += 1
            import time

            self._last_built_at = time.time()
        log.info("人脸索引已重建：%d 人 / %d 条模板", len({e.owner_id for e in entries}), len(entries))
        return len(entries)

    # ---------- 读路径 ----------
    def search(self, vec: np.ndarray, top_k: int = 5) -> list[dict]:
        """余弦检索 top-k。返回按相似度降序的候选（同一人取最高分）。"""
        if self._dirty:
            return []
        with self._lock:
            matrix = self._matrix
            entries = self._entries
        if matrix is None or not entries:
            return []
        if vec.shape[0] != DIM:
            return []

        scores = matrix @ vec.astype(np.float32)
        best: dict[tuple[str, int], dict] = {}
        for i, score in enumerate(scores):
            e = entries[i]
            key = (e.owner_type, e.owner_id)
            s = float(score)
            cur = best.get(key)
            if cur is None or s > cur["score"]:
                best[key] = {"score": s, "enrollment_id": e.enrollment_id,
                             "owner_type": e.owner_type, "owner_id": e.owner_id,
                             "summary": e.summary}
        ranked = sorted(best.values(), key=lambda d: d["score"], reverse=True)
        return ranked[: max(1, top_k)]

    def identify(self, vec: np.ndarray, *, threshold: float, margin: float) -> dict:
        """判定式检索：同时施加"绝对阈值"与"top1/top2 间隔"两道闸。

        只判阈值是不够的 —— 底库里若有两位长相接近的同学（双胞胎、或者只是
        特征区分度不够），top1 与 top2 会同时越过阈值，此时无论选谁都有一半
        概率张冠李戴。间隔闸的作用就是在"存在混淆"时主动返回不确定。
        """
        if self._dirty:
            return {"matched": False, "reason": "index_stale", "candidates": []}
        candidates = self.search(vec, top_k=5)
        if not candidates:
            return {"matched": False, "reason": "empty", "candidates": []}

        top = candidates[0]
        second = candidates[1]["score"] if len(candidates) > 1 else 0.0
        gap = float(top["score"] - second)
        out = {"candidates": candidates, "score": round(float(top["score"]), 4),
               "margin": round(gap, 4)}

        if top["score"] < threshold:
            return {**out, "matched": False, "reason": "below_threshold"}
        if gap < margin:
            return {**out, "matched": False, "reason": "ambiguous"}
        return {**out, "matched": True, "reason": "ok",
                "owner_type": top["owner_type"], "owner_id": top["owner_id"],
                "summary": top["summary"]}

    def status(self) -> dict:
        import time

        with self._lock:
            n_tpl = len(self._entries)
            n_person = len({(e.owner_type, e.owner_id) for e in self._entries})
            return {
                # ready 表示"索引已构建且未过期"，空底库同样是合法的就绪状态 ——
                # 若把"没有模板"也判为未就绪，页面会在还没建档时长期显示红色告警，
                # 掩盖掉真正需要关注的"索引过期"（脏）状态。
                "ready": not self._dirty,
                "dirty": self._dirty,
                "empty": self._matrix is None,
                "dim": DIM,
                "templates": n_tpl,
                "persons": n_person,
                "version": self._version,
                "last_built_at": self._last_built_at or None,
                "age_sec": round(time.time() - self._last_built_at, 1) if self._last_built_at else None,
            }


face_index = FaceIndex()
