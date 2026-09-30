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


def analyze_visual(path: Path, cadence: str = "adaptive") -> dict:
    """逐帧跑真实推理，返回行为时间轴与统计。

    ``cadence`` 决定"哪些帧真正进入 analyze()"，这是唯一会改变判定结果的变量：

      - ``adaptive``（视频文件分析，线上 ``video_pipeline`` 用的就是它）
        运动自适应采样。必须与线上保持一致，否则评测的是另一套东西：
        早先这里用固定 0.33s 步长，恰好跳过了 8 秒素材里仅 0.35 秒的推搡动作，
        把"漏报"误判成"算法不行"。
      - ``realtime``（浏览器实时的节奏）
        直接 ``force=False`` 调引擎，把限频(``INFER_FPS_LIMIT``)与运动门控交给
        **线上同一份代码**去决策。之所以不另写一套采样：行为投票是**按帧计数**的
        （``EVENT_VOTE_WINDOW``/``FIGHT_MIN_FRAMES``/``GROUP_BULLY_EDGE_FRAMES``），
        帧间隔一变，同一个窗口对应的真实时长就变，"几次命中才算数"的口径也随之改变。
        只有复用线上决策路径，才能回答"实时链路是否被改坏"。
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
    skipped = 0
    infer_ms: list[float] = []
    decode_sec = 0.0
    prev_gray = None
    last_infer_t = -1e9
    active_until = -1.0
    t0 = time.perf_counter()
    try:
        while True:
            _t_read = time.perf_counter()
            ok, frame = cap.read()
            decode_sec += time.perf_counter() - _t_read
            if not ok:
                break
            tsec = frame_idx / fps if fps else 0.0

            if cadence == "realtime":
                # 与 routers/detect.py 的实时链路完全同一条路径，只是把"墙上时钟"
                # 换成了视频时间轴：实时链路里 t 也恰好是"真实动作经过的时间"，
                # 因此帧间隔语义一致，可直接横向对比。
                res = engine.infer(frame, None, tsec, False, key)
                frame_idx += 1
                if res.skipped:
                    skipped += 1
                    continue
                sampled += 1
            else:
                small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90),
                                   interpolation=cv2.INTER_AREA)
                motion = 1.0 if prev_gray is None else float(np.abs(small - prev_gray).mean()) / 255.0
                prev_gray = small
                if motion >= settings.VIDEO_MOTION_THRESHOLD:
                    active_until = tsec + settings.VIDEO_ACTIVE_HOLD_SEC
                interval = (settings.VIDEO_SAMPLE_ACTIVE_SEC if tsec <= active_until
                            else settings.VIDEO_SAMPLE_IDLE_SEC)

                frame_idx += 1
                if tsec - last_infer_t < interval:
                    continue
                last_infer_t = tsec
                sampled += 1
                res = engine.infer(frame, None, tsec, True, key)

            people_max = max(people_max, int(res.people or 0))
            face_hits += len(res.faces or [])
            infer_ms.append(float(res.infer_ms))
            for b in res.behaviors:
                timeline.append({
                    "t": round(tsec, 2),
                    "type": b.event_type,
                    "label": str(b.detail.get("label") or b.event_type),
                    "conf": round(float(b.confidence), 3),
                    "tracks": list(b.track_ids),
                    "detail": {k: v for k, v in list(b.detail.items())[:8]},
                })
    finally:
        cap.release()
        engine.reset(key)

    counts: dict[str, int] = {}
    for item in timeline:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    arr = np.array(infer_ms) if infer_ms else np.array([0.0])
    return {
        "duration": round(total / fps, 2) if fps else 0.0,
        "resolution": f"{w}x{h}",
        "fps": round(fps, 2),
        "cadence": cadence,
        "frames": frame_idx,
        "skipped": skipped,
        "sampled": sampled,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        # 单帧推理延迟：实时链路能不能跑起来只取决于它，与视频时长无关。
        # 用 p95 而不是均值来判断「撑得住多少路」——均值会被偶尔的快帧拉低，
        # 而实时链路的瓶颈恰恰出现在最慢的那几帧上（背压累积 → 帧队列积压）。
        "infer_ms_mean": round(float(arr.mean()), 1),
        "infer_ms_p95": round(float(np.percentile(arr, 95)), 1),
        "infer_ms_max": round(float(arr.max()), 1),
        "decode_sec": round(decode_sec, 1),
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
    ap.add_argument("--cadence", choices=("adaptive", "realtime"), default="adaptive",
                    help="帧节奏：adaptive=视频文件分析（线上 video_pipeline）；"
                         "realtime=浏览器实时的限频+运动门控。用于回答"
                         "「按帧计数的行为投票在两种节奏下结论是否一致」")
    args = ap.parse_args()

    vdir = Path(args.dir)
    videos = sorted([p for p in vdir.glob("*") if p.suffix.lower() in (".mp4", ".avi", ".mov", ".mkv", ".webm")])
    if not videos:
        print(f"[失败] {vdir} 下没有视频文件")
        return 2

    ffmpeg = find_ffmpeg()
    print("=" * 78)
    print(f"  真实素材端到端评测 · {len(videos)} 个视频 · 帧节奏={args.cadence}")
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

        vis = analyze_visual(v, args.cadence)
        ratio = (vis.get("elapsed_sec") or 0) / max(1e-6, vis.get("duration") or 1)
        print(f"  视觉：{vis.get('resolution')} @ {vis.get('fps')}fps，"
              f"时长 {vis.get('duration')}s，共 {vis.get('frames')} 帧，"
              f"实推理 {vis.get('sampled')} 帧，跳过 {vis.get('skipped')} 帧")
        print(f"        耗时 {vis.get('elapsed_sec')}s，"
              f"处理/视频 = {ratio:.2f}× （>1 表示比实时慢）")
        print(f"        单帧推理 均值 {vis.get('infer_ms_mean')}ms / "
              f"p95 {vis.get('infer_ms_p95')}ms / 峰值 {vis.get('infer_ms_max')}ms"
              f"（纯推理可支撑 {1000.0 / max(1e-6, vis.get('infer_ms_p95') or 1):.1f} fps）")
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
    sum_video = 0.0
    sum_elapsed = 0.0
    worst_p95 = 0.0
    for item in report["videos"]:
        for k, n in (item["visual"].get("counts") or {}).items():
            agg[k] = agg.get(k, 0) + n
        total_speech += len(item["speech"])
        kw_hits += sum(1 for s in item["speech"] if s["keywords"])
        sum_video += float(item["visual"].get("duration") or 0)
        sum_elapsed += float(item["visual"].get("elapsed_sec") or 0)
        worst_p95 = max(worst_p95, float(item["visual"].get("infer_ms_p95") or 0))
    print("  视觉行为合计：" + ("  ".join(f"{k}×{n}" for k, n in sorted(agg.items())) or "无"))
    print(f"  语音转写：{total_speech} 段，其中命中关键词 {kw_hits} 段")
    if sum_video > 0:
        # 离线：整段视频的吞吐；实时：单帧延迟。两者是不同的约束，必须分开看 ——
        # 离线分析允许"慢于实时"（排队跑完即可），实时链路则必须在下一帧到达前出结果。
        print(f"  离线吞吐：{sum_video:.1f}s 视频用了 {sum_elapsed:.1f}s，"
              f"处理/视频 = {sum_elapsed / sum_video:.2f}×"
              f"（按此比例，10 分钟录像约需 {600 * sum_elapsed / sum_video / 60:.0f} 分钟跑完）")
        if worst_p95 > 0:
            print(f"  实时上限：最差单帧 p95 = {worst_p95:.1f}ms "
                  f"→ 单路可达 {1000.0 / worst_p95:.1f} fps，"
                  f"对 {settings.INFER_FPS_LIMIT} fps 的送检上限有 "
                  f"{1000.0 / worst_p95 / settings.INFER_FPS_LIMIT:.1f}× 余量")
    print(f"  完整报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
