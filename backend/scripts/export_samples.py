"""从人工复核结果导出训练样本，形成「报警 → 人工复核 → 再训练」的数据闭环。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe scripts\\export_samples.py --days 90
    .venv\\Scripts\\python.exe scripts\\export_samples.py --out datasets/review --days 180

输出目录结构：
    datasets/review/
      positive/     复核确认（confirmed）的取证截图 —— 正样本
      negative/     复核判为误报（false_positive）的截图 —— 难负样本
      manifest.csv  文件名 / 事件类型 / 置信度 / 复核结论 / 发生时间

为什么这件事重要：
    公开数据集（RWF-2000、RLVS 等）与真实校园场景存在明显的域差异，文献实测
    跨库性能会掉 20~30%。只有用**本场景真实产生的误报样本**做增量训练，才能
    把误报率真正压下去 —— 而这些样本恰恰是系统日常运行中免费获得的。
    这也是「复核闭环」相比单纯统计误报率更进一步的价值：复核结果不只是数字，
    而是下一轮训练的标注。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

# 允许以 `python scripts/xxx.py` 直接运行：先把 backend 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.models import AlarmRecord, DetectionEvent  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = BASE_DIR / "data" / "evidence"


def _first_image(paths_json: str | None) -> Path | None:
    """从取证的 JSON 列表里挑出第一张图片。"""
    if not paths_json:
        return None
    try:
        items = json.loads(paths_json)
    except (TypeError, ValueError):
        return None
    for p in items if isinstance(items, list) else []:
        name = str(p)
        if name.lower().endswith((".jpg", ".jpeg", ".png")):
            full = EVIDENCE_DIR / Path(name).name
            if full.exists():
                return full
    return None


async def export(out_dir: Path, days: int, min_confidence: float) -> None:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    pos_dir = out_dir / "positive"
    neg_dir = out_dir / "negative"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(AlarmRecord.id, AlarmRecord.feedback_label, AlarmRecord.reason,
                   AlarmRecord.evidence_paths, AlarmRecord.created_at,
                   DetectionEvent.event_type)
            .outerjoin(DetectionEvent, AlarmRecord.event_id == DetectionEvent.id)
            .where(AlarmRecord.created_at >= since)
            .where(AlarmRecord.feedback_label.in_(("confirmed", "false_positive")))
            .order_by(AlarmRecord.id)
        )).all()

    manifest: list[dict] = []
    for aid, feedback, reason, ev_paths, created, etype in rows:
        src = _first_image(ev_paths)
        if src is None:
            continue
        bucket = "positive" if feedback == "confirmed" else "negative"
        dst = (pos_dir if bucket == "positive" else neg_dir) / f"alarm{aid}_{src.name}"
        shutil.copy2(src, dst)
        manifest.append({
            "file": f"{bucket}/{dst.name}",
            "alarm_id": aid,
            "event_type": etype or "",
            "feedback": feedback,
            "created_at": created.isoformat() if created else "",
            "reason": (reason or "")[:120],
        })

    csv_path = out_dir / "manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "alarm_id", "event_type",
                                               "feedback", "created_at", "reason"])
        writer.writeheader()
        writer.writerows(manifest)

    print(f"导出完成：正样本 {len(manifest) - sum(1 for m in manifest if m['feedback'] == 'false_positive')} 张，"
          f"难负样本 {sum(1 for m in manifest if m['feedback'] == 'false_positive')} 张")
    print(f"清单：{csv_path}")
    if not manifest:
        print("提示：当前没有复核记录。请先在「报警处置」页对报警做确认/误报复核，")
        print("      复核结论是这里导出样本的唯一来源。")


def main() -> None:
    ap = argparse.ArgumentParser(description="从人工复核结果导出再训练样本")
    ap.add_argument("--out", default=str(BASE_DIR / "datasets" / "review"),
                    help="输出目录（默认 backend/datasets/review）")
    ap.add_argument("--days", type=int, default=90, help="回溯天数（默认 90）")
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="仅导出置信度不低于该值的样本（预留，当前按报警记录导出）")
    args = ap.parse_args()
    asyncio.run(export(Path(args.out), args.days, args.min_confidence))


if __name__ == "__main__":
    main()
