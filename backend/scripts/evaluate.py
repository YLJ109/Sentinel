"""行为识别评测脚本：在人工标注集上统计精确率 / 召回率 / F1。

为什么需要它：校方一定会问"误报率多少"，而这个数字只能来自标注集的量化评测，
不能靠单张样例的观感。本脚本提供可重复的评测流程，便于每次调阈值后回归。

标注清单格式（CSV，带表头，UTF-8）：
    path,label
    samples/fall_01.mp4,fall
    samples/fight_02.mp4,fight
    samples/normal_corridor.mp4,normal
- ``path``：相对脚本运行目录的图片或视频路径
- ``label``：期望行为，取值 fall / fight / argue / smoke / crowd / normal
  （``normal`` 表示该片段不应产生任何行为）

用法：
    python scripts/evaluate.py --manifest samples/manifest.csv --out report.json

说明：静态图片会被连续送入若干帧，以便多帧投票达到确认帧数——
这是评测分类器在单帧样本上的行为，与视频流的逐帧时序一致。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.vision.engine import engine  # noqa: E402

CLASSES = ["fall", "fight", "argue", "smoke", "crowd"]
IMAGE_SUFFIX = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _load_frames(path: Path, image_repeat: int, video_step_sec: float):
    """把样本读成帧序列。图片复制多份以驱动多帧投票。"""
    if path.suffix.lower() in IMAGE_SUFFIX:
        img = cv2.imread(str(path))
        if img is None:
            return []
        return [img] * max(1, image_repeat)

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return []
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    step = max(1, int(round(fps * video_step_sec)))
    frames, idx = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            frames.append(frame)
        idx += 1
    cap.release()
    return frames


def evaluate(manifest: Path, image_repeat: int, video_step_sec: float) -> dict:
    rows = list(csv.DictReader(manifest.open(encoding="utf-8-sig")))
    if not rows:
        raise SystemExit("标注清单为空")

    stats = {c: {"tp": 0, "fp": 0, "fn": 0} for c in CLASSES + ["normal"]}
    samples = []

    for i, row in enumerate(rows):
        raw_path = (row.get("path") or "").strip()
        label = (row.get("label") or "normal").strip().lower()
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            print(f"[跳过] 文件不存在：{raw_path}")
            continue
        if label not in stats:
            print(f"[跳过] 未知标签 {label}（{raw_path}）")
            continue

        frames = _load_frames(path, image_repeat, video_step_sec)
        if not frames:
            print(f"[跳过] 无法读取：{raw_path}")
            continue

        # 每条样本使用独立管线，避免彼此污染跟踪状态
        key = f"eval:{i}"
        engine.reset(key)
        seen: set[str] = set()
        for frame in frames:
            result = engine.infer(frame, camera_id=None, timestamp=None, force=True, pipeline_key=key)
            seen.update(h.event_type for h in result.behaviors)
        engine.reset(key)

        hit = label in seen or (label == "normal" and not (seen & set(CLASSES)))
        samples.append({"path": raw_path, "label": label, "predicted": sorted(seen), "correct": hit})

        for c in CLASSES:
            if label == c and c in seen:
                stats[c]["tp"] += 1
            elif label == c:
                stats[c]["fn"] += 1
            elif c in seen:
                stats[c]["fp"] += 1
        if label == "normal" and not (seen & set(CLASSES)):
            stats["normal"]["tp"] += 1
        elif label == "normal":
            stats["normal"]["fn"] += 1

    report = {"classes": {}, "samples": samples,
              "config": {"imgsz": settings.IMGSZ, "image_repeat": image_repeat}}

    print("=" * 74)
    print(f"{'类别':<8}{'TP':>5}{'FP':>5}{'FN':>5}{'精确率':>10}{'召回率':>10}{'F1':>8}")
    print("-" * 74)
    for c, s in stats.items():
        tp, fp, fn = s["tp"], s["fp"], s["fn"]
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        report["classes"][c] = {"tp": tp, "fp": fp, "fn": fn,
                                "precision": round(prec, 4), "recall": round(rec, 4),
                                "f1": round(f1, 4)}
        print(f"{c:<8}{tp:>5}{fp:>5}{fn:>5}{prec:>10.3f}{rec:>10.3f}{f1:>8.3f}")

    total = len(samples)
    correct = sum(1 for s in samples if s["correct"])
    report["overall"] = {"samples": total, "correct": correct,
                         "accuracy": round(correct / total, 4) if total else 0.0}
    print("-" * 74)
    print(f"样本 {total} 条，整体判定正确 {correct} 条"
          f"（{report['overall']['accuracy'] * 100:.1f}%）")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="行为识别评测")
    parser.add_argument("--manifest", required=True, help="标注清单 CSV")
    parser.add_argument("--out", default="", help="评测报告输出路径（JSON）")
    parser.add_argument("--image-repeat", type=int, default=12,
                        help="静态图片重复送检帧数（需 >= 各行为的确认帧数）")
    parser.add_argument("--video-step-sec", type=float, default=0.2,
                        help="视频抽帧间隔（秒）")
    args = parser.parse_args()

    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"[错误] 标注清单不存在：{manifest}", file=sys.stderr)
        return 2

    report = evaluate(manifest, args.image_repeat, args.video_step_sec)
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写出：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
