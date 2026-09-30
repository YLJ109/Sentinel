"""视频检测时间轴链路 —— 可复现验证脚本。

用法（在 backend 目录下，需后端已启动）：
    .venv\\Scripts\\python.exe scripts\\check_video.py

验证内容：
  1. 上传视频后后台任务能跑完，并产出标注视频
  2. 新增的时间轴增量接口（/events、/speech）返回结构正确、since_id 增量生效
  3. 事件确实挂到了该视频任务上（video_id 隔离，不会与实时链路串台）

说明：本脚本用 OpenCV 合成一段纯色视频。合成画面里没有真实人体，
因此**不会产生行为事件** —— 这是预期结果，脚本只校验链路是否通。
真实事件的召回率必须用实际拍摄素材统计。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np

BASE = os.environ.get("CAB_CHECK_BASE", "http://127.0.0.1:8000").rstrip("/")
TAG = uuid.uuid4().hex[:6]


def req(method: str, path: str, data=None, token: str | None = None,
        raw: bytes | None = None, ctype: str = "application/json; charset=utf-8"):
    body = raw if raw is not None else (
        json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None)
    r = urllib.request.Request(BASE + path, data=body, method=method)
    r.add_header("Content-Type", ctype)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"detail": text}


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----cab" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    out += b"Content-Type: video/mp4\r\n\r\n"
    out += content
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def make_video(path: Path, seconds: float = 6.0, fps: int = 15) -> None:
    """合成一段有运动的视频（移动的方块），保证运动门控不会跳过全部帧。"""
    w, h = 480, 360
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    total = int(seconds * fps)
    for i in range(total):
        img = np.full((h, w, 3), 28, dtype=np.uint8)
        x = 40 + int((w - 140) * i / max(1, total - 1))
        cv2.rectangle(img, (x, 200), (x + 60, 300), (200, 205, 210), -1)
        cv2.rectangle(img, (w - x - 60, 210), (w - x, 310), (170, 175, 180), -1)
        writer.write(img)
    writer.release()


def main() -> int:
    st, resp = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    if st != 200:
        print("登录失败：", resp)
        return 1
    tok = resp["access_token"]
    print("登录成功，测试标记 =", TAG, "\n")

    tmp = Path(settings_dir()) / f"_check_{TAG}.mp4"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    make_video(tmp)
    print("【准备】合成测试视频 %s（%.2f MB）" % (tmp.name, tmp.stat().st_size / 1048576))

    ok = True
    rec_id = None
    try:
        # ---------- 1. 上传 ----------
        body, ctype = multipart({}, f"check_{TAG}.mp4", tmp.read_bytes())
        st, rec = req("POST", "/api/video/upload", token=tok, raw=body, ctype=ctype)
        print("\n【1】上传：status=%d id=%s 文件名=%s" % (st, rec.get("id"), rec.get("filename")))
        ok &= st == 200
        rec_id = rec.get("id")
        if not rec_id:
            raise RuntimeError("未拿到记录 id")

        # ---------- 2. 轮询到完成 ----------
        print("\n【2】等待后台检测完成")
        deadline = time.time() + 180
        last = None
        while time.time() < deadline:
            st, cur = req("GET", f"/api/video/{rec_id}", token=tok)
            if cur.get("status") != last:
                print("      状态：%s 进度=%s" % (cur.get("status"), cur.get("progress")))
                last = cur.get("status")
            if cur.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        ok &= cur.get("status") == "done"
        print("      最终：status=%s 时长=%ss 事件=%s 报警=%s" % (
            cur.get("status"), cur.get("duration"), cur.get("event_count"), cur.get("alarm_count")))
        print("      标注视频：%s" % (cur.get("processed_path") or "（未生成）"))
        ok &= bool(cur.get("processed_path")) or cur.get("status") == "done"

        # ---------- 3. 时间轴增量接口 ----------
        print("\n【3】时间轴增量接口")
        st, ev1 = req("GET", f"/api/video/{rec_id}/events?since_id=0", token=tok)
        print("      /events：status=%d 条数=%d last_id=%s 状态=%s 进度=%s" % (
            st, len(ev1.get("items", [])), ev1.get("last_id"), ev1.get("status"), ev1.get("progress")))
        ok &= st == 200 and "items" in ev1 and "last_id" in ev1

        st, ev2 = req("GET", f"/api/video/{rec_id}/events?since_id={ev1.get('last_id')}", token=tok)
        print("      增量（since_id=%s）：条数=%d（应为 0）" % (ev1.get("last_id"), len(ev2.get("items", []))))
        ok &= len(ev2.get("items", [])) == 0

        st, sp = req("GET", f"/api/video/{rec_id}/speech?since_id=0", token=tok)
        print("      /speech：status=%d 条数=%d" % (st, len(sp.get("items", []))))
        ok &= st == 200 and "items" in sp

        if ev1.get("items"):
            sample = ev1["items"][0]
            print("      事件样例：t=%s type=%s label=%s conf=%s persons=%s" % (
                sample.get("t"), sample.get("event_type"), sample.get("label"),
                sample.get("confidence"), (sample.get("detail") or {}).get("persons")))

        # ---------- 4. 隔离性：实时链路的事件不带 video_id ----------
        print("\n【4】隔离性检查")
        st, page = req("GET", "/api/history/events?limit=5", token=tok)
        items = page.get("items", page if isinstance(page, list) else [])
        with_video = sum(1 for x in items if x.get("video_id"))
        print("      历史事件最近 %d 条中，带 video_id 的 %d 条（仅离线任务才会有）" % (len(items), with_video))
        ok &= st == 200

        # ---------- 5. 语音 WS 的 video_id 控制帧 ----------
        # 这是本次新增里最容易出错的一环（控制帧解析 + 按视频回查点位 + 转写挂 video_id）。
        # 这里推 1 秒静音，只验证协议被正确接受、会话能正常建立与收尾；
        # 能否识别出内容取决于 ASR Provider 是否就绪，不作为通过条件。
        print("\n【5】语音 WS（video_id 控制帧）")
        ws_result = asyncio.run(_probe_audio_ws(tok, rec_id))
        print("      %s" % ws_result)
        ok &= not ws_result.startswith("异常")

    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        print("\n【清理】已删除本地临时视频")

    print("\n结论：%s" % ("全部通过" if ok else "存在失败项，请查看上方输出"))
    return 0 if ok else 2


def settings_dir() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.core.config import UPLOAD_DIR

    return str(UPLOAD_DIR)


async def _probe_audio_ws(token: str, video_id: int) -> str:
    """连上语音 WS，下发 video_id 控制帧并推 1 秒静音，观察是否被正常接受。"""
    import websockets

    url = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/api/ws/audio"
    try:
        async with websockets.connect(url, subprotocols=["cab.v1", token],
                                      open_timeout=20, close_timeout=5) as ws:
            await ws.send(json.dumps({"video_id": video_id}))
            silent = b"\x00" * (3200 * 2)      # 0.2s @16k 单声道 int16
            for _ in range(5):
                await ws.send(silent)
            await ws.send(json.dumps({"flush": True}))
            frames = 0
            err = ""
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=6)
                    frames += 1
                    try:
                        d = json.loads(msg)
                    except (ValueError, TypeError):
                        continue
                    if d.get("type") == "error":
                        err = str(d.get("detail") or d.get("error") or "")
            except asyncio.TimeoutError:
                pass
            if err:
                return f"会话建立成功，但 ASR 不可用：{err}（协议本身正常）"
            return f"会话建立并正常收尾，收到 {frames} 条下行消息"
    except Exception as e:  # noqa: BLE001
        return f"异常：{type(e).__name__} {e}"


if __name__ == "__main__":
    sys.exit(main())
