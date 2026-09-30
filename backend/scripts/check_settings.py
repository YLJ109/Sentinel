"""系统设置（可调参数）—— 可复现验证脚本。

用法（在 backend 目录下，需后端已启动）：
    .venv\\Scripts\\python.exe scripts\\check_settings.py

验证四件事：
  1. 参数清单能正常下发（分组、范围、当前值）
  2. 修改后**立即生效**（直接作用于 settings 对象，引擎下一帧就读到）
  3. 越界值被钳制、白名单外的键被拒绝
  4. 重置能恢复为 .env 基线（而不是代码常量）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# 允许用环境变量指向非默认端口（本机常有其他项目同时占用 8000）
BASE = os.environ.get("CAB_CHECK_BASE", "http://127.0.0.1:8000").rstrip("/")

# 本次测试使用的参数（结束时会重置，不影响现场标定）
PROBE_KEY = "FIGHT_WRIST_SPEED"
PROBE_VALUE = 0.88


def req(method: str, path: str, data: dict | None = None, token: str | None = None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, method=method)
    r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw}


def find_item(schema: dict, key: str) -> dict | None:
    for g in schema.get("groups", []):
        for it in g.get("items", []):
            if it["key"] == key:
                return it
    return None


def main() -> int:
    st, resp = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    if st != 200:
        print("登录失败：", resp)
        return 1
    tok = resp["access_token"]
    print("登录成功\n")

    # ---------- 1. 参数清单 ----------
    _, schema = req("GET", "/api/settings/schema", token=tok)
    groups = schema["groups"]
    total = sum(len(g["items"]) for g in groups)
    print("【1】参数清单：%d 个分组 / %d 个可调参数" % (len(groups), total))
    for g in groups:
        print("      %-10s %d 项" % (g["label"], len(g["items"])))
    print("      只读项 %d 个（需重载模型才能变，不开放编辑）" % len(schema["readonly"]))

    item = find_item(schema, PROBE_KEY)
    if not item:
        print("  未找到探针参数 %s：%s" % (PROBE_KEY, "失败"))
        return 2
    baseline = item["value"]
    print("      探针参数 %s 当前值 = %s" % (PROBE_KEY, baseline))

    # ---------- 2. 修改并验证立即生效 ----------
    st, r = req("PATCH", "/api/settings", {"values": {PROBE_KEY: PROBE_VALUE}}, token=tok)
    _, schema2 = req("GET", "/api/settings/schema", token=tok)
    after = find_item(schema2, PROBE_KEY)["value"]
    print("\n【2】修改参数：status=%d 下发值=%s → 实际生效值=%s（期望 %s）"
          % (st, PROBE_VALUE, after, PROBE_VALUE))
    ok_apply = abs(float(after) - PROBE_VALUE) < 1e-6

    # ---------- 3. 越界钳制与白名单 ----------
    st, r = req("PATCH", "/api/settings", {"values": {PROBE_KEY: 999}}, token=tok)
    _, schema3 = req("GET", "/api/settings/schema", token=tok)
    clamped = find_item(schema3, PROBE_KEY)["value"]
    print("【3】越界值 999 → 实际 %s（应被钳制到上限 %s）" % (clamped, item["max"]))
    ok_clamp = float(clamped) == float(item["max"])

    st, r = req("PATCH", "/api/settings", {"values": {"SECRET_KEY": "hacked"}}, token=tok)
    failed = r.get("failed") or []
    print("【4】白名单外参数 SECRET_KEY → 被拒 %d 项，原因：%s"
          % (len(failed), failed[0]["reason"] if failed else "-"))
    ok_guard = len(failed) == 1

    # ---------- 5. 重置回 .env 基线 ----------
    st, r = req("POST", "/api/settings/reset?names=" + PROBE_KEY, token=tok)
    _, schema4 = req("GET", "/api/settings/schema", token=tok)
    restored = find_item(schema4, PROBE_KEY)["value"]
    print("\n【5】重置后 %s = %s（应回到基线 %s）" % (PROBE_KEY, restored, baseline))
    ok_reset = abs(float(restored) - float(baseline)) < 1e-6

    # ---------- 6. 持久化：确认已落库 ----------
    _, runtime = req("GET", "/api/settings/runtime", token=tok)
    print("【6】当前覆盖项：%s（重置后应为空）" % (runtime.get("overrides") or "无"))
    ok_persist = not runtime.get("overrides")

    print("\n结论：立即生效 %s / 越界钳制 %s / 白名单 %s / 重置回基线 %s"
          % ("通过" if ok_apply else "失败",
             "通过" if ok_clamp else "失败",
             "通过" if ok_guard else "失败",
             "通过" if ok_reset else "失败"))
    return 0 if (ok_apply and ok_clamp and ok_guard and ok_reset and ok_persist) else 2


if __name__ == "__main__":
    raise SystemExit(main())
