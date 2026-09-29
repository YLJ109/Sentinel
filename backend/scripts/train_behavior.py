"""自定义行为模型训练脚本（吸烟 / 打架 / 倒地等 COCO 之外的类别）。

用法：
    # 1) 准备数据集：按 datasets/behaviors/data.yaml 的说明放置 images/labels
    # 2) 训练
    .venv\\Scripts\\python scripts\\train_behavior.py --data datasets/behaviors/data.yaml --epochs 100
    # 3) 训练完成后权重会复制到 backend/models/，在 .env 中启用：
    #    CAB_YOLO_BEHAVIOR_WEIGHTS=behaviors_best.pt

为什么需要单独训练：
    YOLOv8 官方权重基于 COCO 80 类，其中没有"香烟""打架"这类行为语义。
    系统对吸烟采用姿态代理特征兜底，若要把误报率进一步压下来，
    需要用校园实拍数据训练专用类别，再通过本脚本产出权重接入推理链路。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 允许以 `python scripts/train_behavior.py` 方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.vision.registry import resolve_device  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="训练自定义行为检测模型")
    p.add_argument("--data", default=settings.TRAIN_DATASET_YAML, help="数据集 yaml 路径")
    p.add_argument("--base", default=settings.TRAIN_BASE_WEIGHTS, help="基座权重（迁移学习起点）")
    p.add_argument("--epochs", type=int, default=settings.TRAIN_EPOCHS)
    p.add_argument("--batch", type=int, default=settings.TRAIN_BATCH)
    p.add_argument("--imgsz", type=int, default=settings.IMGSZ)
    p.add_argument("--device", default="auto", help="auto / 0 / cpu")
    p.add_argument("--name", default="behaviors", help="训练任务名，决定输出文件名")
    p.add_argument("--export-onnx", action="store_true", help="额外导出 ONNX（规避 AGPL 运行时依赖时使用）")
    return p


def main() -> int:
    args = build_parser().parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"[错误] 数据集配置不存在：{data_path}", file=sys.stderr)
        return 2

    from ultralytics import YOLO

    device = resolve_device() if args.device == "auto" else args.device
    print(f"[信息] 数据集={data_path} 基座={args.base} device={device} epochs={args.epochs}")

    model = YOLO(args.base)
    model.train(
        data=str(data_path),
        epochs=args.epochs,
        batch=args.batch,
        imgsz=args.imgsz,
        device=device,
        project=str(settings.MODELS_DIR / "runs"),
        name=args.name,
        exist_ok=True,
        pretrained=True,
        # 小数据集下开启较强增强，抑制过拟合
        mosaic=1.0,
        mixup=0.1,
        degrees=10.0,
        hsv_v=0.4,
    )

    # 验证并输出指标
    metrics = model.val(data=str(data_path), device=device, imgsz=args.imgsz)
    map50 = float(getattr(metrics.box, "map50", 0.0))
    print(f"[结果] mAP@0.5 = {map50:.4f}")

    best = Path(model.trainer.save_dir) / "weights" / "best.pt"
    if not best.exists():
        print(f"[错误] 未找到训练产物：{best}", file=sys.stderr)
        return 1

    target = settings.MODELS_DIR / f"{args.name}_best.pt"
    shutil.copy2(best, target)
    print(f"[完成] 权重已导出：{target}")
    print(f"[启用] 在 backend/.env 设置 CAB_YOLO_BEHAVIOR_WEIGHTS={target.name} 后重启服务")

    if args.export_onnx:
        onnx_path = model.export(format="onnx", imgsz=args.imgsz, device="cpu", half=False)
        print(f"[完成] ONNX 已导出：{onnx_path}（可用 ONNX Runtime / TensorRT 独立推理）")

    if map50 < 0.5:
        print("[提醒] mAP@0.5 低于 0.5，建议增加样本量或检查标注质量后再上线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
