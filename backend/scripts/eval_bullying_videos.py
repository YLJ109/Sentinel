"""真实素材评测：对上传的校园霸凌视频跑完整链路（视觉 + 语音），输出可复核的结果。

为什么单独写一个脚本而不是用 evaluate.py：
    evaluate.py 面向"人工标注的单个行为片段"算 P/R/F1；
    而真实成片里**一段视频包含多种混合行为**（推搡 + 踢 + 辱骂 + 围观），
    标注粒度完全不同。这里按"逐帧输出时间轴"的方式评测，
    既能看命中，也能看漏报与误报具体发生在第几秒。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe scripts\\eval_bullying_videos.py --dir data/eval

依赖：ffmpeg（用于从 mp4/avi/mkv 抽取音频轨）。缺失时自动跳过语音部分，
不影响视觉评测 —— 与线上"视觉不依赖语音"的设计一致。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.audio.asr import asr  # noqa: E402
from app.audio.keywords import scan  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.vision.engine import engine  # noqa: E402



def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for p in Path("F:/Program Files").glob("ffmpeg*/bin/ffmpeg.exe"):
        return str(p)
    return None


def extract_audio(video: Path, out_wav: Path, ffmpeg: str) -> bool:
    """抽音频并统一转成 ASR 要求的 16kHz / 单声道 / 16bit PCM。"""
    cmd = [ffmpeg, "-y", "-i", str(video), "-vn",
           "-ac", "1", "-ar", str(settings.SPEECH_SAMPLE_RATE),
           "-acodec", "pcm_s16le", "-f", "wav", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 44


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


async def transcribe(wav: Path) -> list[dict]:
    """把整段音频喂给 ASR，返回 [{t, text, keywords}]。"""
    pcm = load_wav(wav)
    session = asr.open_session()
    segments = []
    try:
        for i in range(0, len(pcm), 3200):
            segments.extend(await session.accept(pcm[i:i + 3200]))
        segments.extend(await session.finalize())
    finally:
        await session.close()

    out: list[dict] = []
    for s in segments:
        if s.partial or not s.text.strip():
            continue
        hits = scan(s.text)
        out.append({"t": round(float(s.start), 2), "text": s.text,
                    "keywords": [h.keyword for h in hits],
                    "levels": sorted({h.level for h in hits})})
    return out


def analyze_visual(path: Path) -> dict:
    """逐帧跑真实推理，返回行为时间轴与统计。

    采样策略必须与线上 ``video_pipeline`` 保持一致（运动自适应），
    否则评测的是另一套东西：早先这里用固定 0.33s 步长，
    恰好跳过了 8 秒素材里仅 0.35 秒的推搡动作，把"漏报"误判成"算法不行"。
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {"error": "视频无法打开"}
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

    key = f"eval:{path.stem}"
    timeline: list[dict] = []
    frame_idx = 0
    people_max = 0
    face_hits = 0
    sampled = 0
    prev_gray = None
    last_infer_t = -1e9
    active_until = -1.0
    t0 = time.perf_counter()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            tsec = frame_idx / fps if fps else 0.0

            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90),
                               interpolation=cv2.INTER_AREA)
            motion = 1.0 if prev_gray is None else float(np.abs(small - prev_gray).mean()) / 255.0
            prev_gray = small
            if motion >= settings.VIDEO_MOTION_THRESHOLD:
                active_until = tsec + settings.VIDEO_ACTIVE_HOLD_SEC
            interval = (settings.VIDEO_SAMPLE_ACTIVE_SEC if tsec <= active_until
                        else settings.VIDEO_SAMPLE_IDLE_SEC)

            if tsec - last_infer_t >= interval:
                last_infer_t = tsec
                sampled += 1
                res = engine.infer(frame, None, tsec, True, key)
                people_max = max(people_max, int(res.people or 0))
                face_hits += len(res.faces or [])
                for b in res.behaviors:
                    timeline.append({
                        "t": round(tsec, 2),
                        "type": b.event_type,
                        "label": str(b.detail.get("label") or b.event_type),
                        "conf": round(float(b.confidence), 3),
                        "tracks": list(b.track_ids),
                        "detail": {k: v for k, v in list(b.detail.items())[:8]},
                    })
            frame_idx += 1
    finally:
        cap.release()
        engine.reset(key)

    counts: dict[str, int] = {}
    for item in timeline:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {
        "duration": round(total / fps, 2) if fps else 0.0,
        "resolution": f"{w}x{h}",
        "fps": round(fps, 2),
        "sampled": sampled,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "people_max": people_max,
        "face_detections": face_hits,
        "counts": counts,
        "timeline": timeline,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="真实霸凌素材端到端评测")
    ap.add_argument("--dir", default="data/eval", help="视频目录")
    ap.add_argument("--out", default="data/eval/report.json", help="报告输出路径")
    ap.add_argument("--no-audio", action="store_true", help="跳过语音识别")
    args = ap.parse_args()

    vdir = Path(args.dir)
    videos = sorted([p for p in vdir.glob("*") if p.suffix.lower() in (".mp4", ".avi", ".mov", ".mkv", ".webm")])
    if not videos:
        print(f"[失败] {vdir} 下没有视频文件")
        return 2

    ffmpeg = find_ffmpeg()
    print("=" * 78)
    print(f"  真实素材端到端评测 · {len(videos)} 个视频")
    print(f"  ffmpeg：{ffmpeg or '未找到（将跳过语音评测）'}")
    print("=" * 78)

    if ffmpeg and not args.no_audio:
        try:
            await asr.prepare()
            print(f"[语音] ASR 已就绪：{asr.status().get('provider')}")
        except Exception as e:  # noqa: BLE001
            print(f"[语音] ASR 不可用，跳过语音评测：{e}")
            ffmpeg = None

    report = {"videos": []}
    for v in videos:
        print("\n" + "-" * 78)
        print(f"▶ {v.name}  ({v.stat().st_size / 1048576:.2f} MB)")
        print("-" * 78)

        vis = analyze_visual(v)
        print(f"  视觉：{vis.get('resolution')} @ {vis.get('fps')}fps，"
              f"时长 {vis.get('duration')}s，采样 {vis.get('sampled')} 帧，"
              f"耗时 {vis.get('elapsed_sec')}s")
        print(f"        人数峰值 {vis.get('people_max')}，人脸框累计 {vis.get('face_detections')}")
        if vis.get("counts"):
            print("        行为统计：" + "  ".join(f"{k}×{n}" for k, n in sorted(vis["counts"].items())))
        else:
            print("        行为统计：**未检出任何行为**")

        # 按时间轴打印前 24 条，便于逐条核对（超出部分只在报告里保留）
        for item in vis.get("timeline", [])[:24]:
            keys = " ".join(f"{k}={v}" for k, v in item["detail"].items()
                            if isinstance(v, (int, float)))
            print(f"          {item['t']:>6.2f}s  {item['label']:<6} conf={item['conf']:<5} "
                  f"#{item['tracks']}  {keys}")

        speech: list[dict] = []
        if ffmpeg:
            with tempfile.TemporaryDirectory() as td:
                wav = Path(td) / f"{v.stem}.wav"
                if extract_audio(v, wav, ffmpeg):
                    try:
                        speech = await transcribe(wav)
                    except Exception as e:  # noqa: BLE001
                        print(f"  语音：识别失败 {e}")
                else:
                    print("  语音：音频抽取失败（该视频可能没有音频轨）")
            if speech:
                print(f"  语音：{len(speech)} 段转写")
                for s in speech:
                    mark = f"  ★命中 {'、'.join(s['keywords'])}" if s["keywords"] else ""
                    print(f"          {s['t']:>6.2f}s  「{s['text']}」{mark}")
            elif ffmpeg:
                print("  语音：未识别出内容")

        report["videos"].append({"file": v.name, "visual": vis, "speech": speech})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("  汇总")
    print("=" * 78)
    agg: dict[str, int] = {}
    total_speech = 0
    kw_hits = 0
    for item in report["videos"]:
        for k, n in (item["visual"].get("counts") or {}).items():
            agg[k] = agg.get(k, 0) + n
        total_speech += len(item["speech"])
        kw_hits += sum(1 for s in item["speech"] if s["keywords"])
    print("  视觉行为合计：" + ("  ".join(f"{k}×{n}" for k, n in sorted(agg.items())) or "无"))
    print(f"  语音转写：{total_speech} 段，其中命中关键词 {kw_hits} 段")
    print(f"  完整报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
