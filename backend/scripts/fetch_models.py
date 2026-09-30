"""下载人脸识别与情绪识别所需的模型权重到 backend/models/。

用法：
    .venv\\Scripts\\python.exe scripts\\fetch_models.py

为什么需要单独一个下载脚本：这些权重体积较大（合计约 70MB），
已按 .gitignore 排除，不进版本库；部署到新机器时必须能一键补齐，
否则人脸识别会静默降级为"只检测不识别"。

权重来源与授权：
- face_recognition_sface_2021dec.onnx —— OpenCV Zoo，**Apache 2.0，可商用**
- face_detection_yunet_2023mar.onnx   —— OpenCV Zoo，**Apache 2.0，可商用**
- emotion-ferplus-8.onnx              —— ONNX Model Zoo，**MIT**

本项目刻意不使用 InsightFace 的 buffalo 系列权重：其代码虽为 MIT，
但预训练模型官方声明「仅供非商业学术研究」，校园实际部署会有授权风险。
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402

# (文件名, 下载地址, 说明)
MODELS: list[tuple[str, str, str]] = [
    (
        "face_detection_yunet_2023mar.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "YuNet 人脸检测（约 0.2MB）",
    ),
    (
        "face_recognition_sface_2021dec.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "SFace 人脸特征（约 37MB，128 维）",
    ),
    (
        "emotion-ferplus-8.onnx",
        "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx",
        "FER+ 表情识别（约 33MB，8 类）",
    ),
]


def download(name: str, url: str, note: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"  [跳过] {name} 已存在（{dest.stat().st_size / 1048576:.2f} MB）")
        return True
    print(f"  [下载] {note}")
    print(f"         {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r         进度 {got * 100 // total:3d}%", end="", flush=True)
        print(f"\r         完成 {dest.stat().st_size / 1048576:.2f} MB")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"\r         [失败] {e}")
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def main() -> int:
    out = Path(settings.MODELS_DIR)
    out.mkdir(parents=True, exist_ok=True)
    print("=" * 66)
    print("  守望 Sentinel · 人脸识别与情绪识别模型下载")
    print(f"  目标目录：{out}")
    print("=" * 66)

    ok = True
    for name, url, note in MODELS:
        ok &= download(name, url, note, out / name)

    print("=" * 66)
    if ok:
        print("  全部就绪。重启后端即可启用「人脸识别」与「情绪识别」能力。")
    else:
        print("  存在下载失败项。可手动下载后放入上述目录（文件名须一致）。")
        print("  注意：模型缺失不会影响行为检测与报警，只是对应能力降级。")
    print("=" * 66)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
