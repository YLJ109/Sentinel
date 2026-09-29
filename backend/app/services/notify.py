"""报警外发通知：把高等级报警推送到企业微信 / 钉钉 / 自建网关。

为什么放在服务端而不是只靠浏览器通知：
浏览器通知只在值班页面打开时有效，无法覆盖"无人盯屏"的时段。
服务端 Webhook 可以推到值班群，做到真正意义上的"推送提醒"。

消息体兼容性：
企业微信机器人与钉钉机器人的文本消息格式一致（``msgtype/text/content``），
其他网关则回退为通用 ``{"text": ...}`` 结构，便于自建服务解析。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from app.core.config import settings

log = logging.getLogger("services.notify")

_LEVEL_ORDER = {"low": 1, "medium": 2, "high": 3}


def _should_notify(level: str) -> bool:
    if not settings.NOTIFY_WEBHOOK_URL:
        return False
    return _LEVEL_ORDER.get(level, 0) >= _LEVEL_ORDER.get(settings.NOTIFY_MIN_LEVEL, 3)


def _build_payload(alarm: dict) -> dict:
    label = {"high": "高危", "medium": "中警", "low": "提示"}.get(alarm.get("level", ""), alarm.get("level", ""))
    source = {"video": "视觉", "audio": "语音", "manual": "人工"}.get(alarm.get("source", ""), alarm.get("source", ""))
    text = (f"【校园反霸凌报警·{label}】\n"
            f"来源：{source}\n"
            f"原因：{alarm.get('reason', '')}\n"
            f"报警编号：#{alarm.get('alarm_id', '')}\n"
            f"请值班人员及时处置。")

    # 统一采用企业微信/钉钉的文本消息结构（两者格式一致），
    # 同时附带结构化 alarm 字段，便于自建网关直接解析而不必再解析文本。
    return {"msgtype": "text", "text": {"content": text}, "alarm": alarm}


def _post(url: str, payload: dict) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=settings.NOTIFY_TIMEOUT_SEC) as resp:
        return resp.status


async def notify_alarm(alarm: dict) -> bool:
    """异步推送一条报警。失败只记日志，绝不影响主流程。"""
    if not _should_notify(alarm.get("level", "")):
        return False
    payload = _build_payload(alarm)
    try:
        status = await asyncio.to_thread(_post, settings.NOTIFY_WEBHOOK_URL, payload)
        log.info("报警 #%s 已外发通知，HTTP %s", alarm.get("alarm_id"), status)
        return True
    except Exception as e:
        log.warning("报警外发通知失败（不影响报警落库）：%s", e)
        return False
