"""时间解析工具。

各路由原先各自复制了一份 ``_parse_dt``（报警页与历史取证页各一份，实现完全相同），
这里抽出唯一实现，避免两处行为在后续维护中悄悄分叉。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status


def parse_dt_query(raw: str | None) -> datetime | None:
    """把查询参数里的时间字符串转成与数据库一致的 naive UTC 时间。

    接受两种形式：
    - 完整 ISO（带或不带时区偏移），如 ``2026-09-30T00:00:00+08:00``、``...Z``
    - 纯日期 ``YYYY-MM-DD``（``fromisoformat`` 会补成当天 00:00:00）

    注意：**不带偏移的输入按 UTC 解释**，与库内存储口径一致。
    前端必须发送带偏移的 ISO（例如用 ``new Date(...).toISOString()``），
    否则本地日界会被当成 UTC 日界，跨时区部署时会漏查一个时区内的数据。
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"时间格式不正确：{raw}") from None
    # 数据库存的是 naive 时间，统一去掉时区便于比较
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
