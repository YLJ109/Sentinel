"""云端语音识别自检脚本。

用途：填好凭据后，用一条命令确认"鉴权 + 实时识别 + 关键词命中"整条链路可用，
避免把凭据问题带到正式运行环境里才发现。

用法：
    # 1) 生成一段测试语音（Windows 自带中文语音，仅用于自测）
    powershell -c "Add-Type -AssemblyName System.Speech; \
        $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; \
        $s.SelectVoice('Microsoft Huihui Desktop'); \
        $f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, \
            [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, \
            [System.Speech.AudioFormat.AudioChannel]::Mono); \
        $s.SetOutputToWaveFile('asr_check.wav', $f); \
        $s.Speak('你再这样我就打你了。救命啊老师快来。'); $s.Dispose()"

    # 2) 自检（可用 --wav 指定其它 16k 单声道 PCM WAV）
    python scripts/check_asr.py --wav asr_check.wav

退出码：0 全部通过；1 识别为空；2 配置或网络错误。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audio.asr import asr                      # noqa: E402
from app.audio.keywords import scan                # noqa: E402
from app.core.config import settings               # noqa: E402


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        if rate != settings.SPEECH_SAMPLE_RATE or ch != 1 or width != 2:
            print(f"[警告] 音频为 {rate}Hz/{ch}声道/{width*8}bit，"
                  f"云端要求 {settings.SPEECH_SAMPLE_RATE}Hz/单声道/16bit，识别质量可能受影响")
        return wf.readframes(wf.getnframes())


async def main() -> int:
    parser = argparse.ArgumentParser(description="ASR 链路自检")
    parser.add_argument("--wav", default="asr_check.wav", help="16kHz 单声道 16bit WAV")
    args = parser.parse_args()

    status = asr.status()
    print("=" * 68)
    print(f"Provider : {status.get('provider')}")
    print(f"引擎     : {status.get('engine')}")
    print(f"就绪     : {status.get('ready')}")
    if status.get("credential_mode"):
        print(f"凭据模式 : {status.get('credential_mode')}")
    print("=" * 68)

    t0 = time.perf_counter()
    try:
        await asr.prepare()
    except Exception as e:
        print(f"[失败] 引擎就绪失败：{e}")
        print("       检查 backend/.env 中该 Provider 对应的凭据，"
              "或改用 CAB_ASR_PROVIDER=vosk")
        return 2
    print(f"[通过] 引擎就绪，耗时 {time.perf_counter()-t0:.2f}s")

    wav = Path(args.wav)
    if not wav.exists():
        print(f"[失败] 找不到测试音频：{wav}（请先按文件头部注释生成）")
        return 2

    pcm = load_wav(wav)
    print(f"[信息] 音频 {len(pcm)/2/settings.SPEECH_SAMPLE_RATE:.2f}s，"
          f"按 100ms 分块模拟流式送入")

    session = asr.open_session()
    segments = []
    try:
        for i in range(0, len(pcm), 3200):
            segments.extend(await session.accept(pcm[i:i + 3200]))
        segments.extend(await session.finalize())
    finally:
        await session.close()

    finals = [s for s in segments if not s.partial and s.text.strip()]
    partials = [s for s in segments if s.partial and s.text.strip()]
    print(f"[信息] 中间结果 {len(partials)} 次，成句结果 {len(finals)} 段")

    if not finals:
        print("[失败] 未识别出任何成句文本")
        return 1

    hits = []
    for s in finals:
        h = scan(s.text)
        hits.extend(h)
        print(f"       「{s.text}」 conf={s.confidence:.2f} "
              f"{'命中：' + '、'.join(x.keyword for x in h) if h else ''}")

    if hits:
        print(f"[通过] 关键词命中：{'、'.join(sorted({h.keyword for h in hits}))}")
    else:
        print("[提醒] 识别成功但未命中关键词（若测试语音含敏感词，说明需要调关键词表或换模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
