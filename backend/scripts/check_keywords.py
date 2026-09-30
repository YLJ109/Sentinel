"""关键词管理与三档响应 —— 可复现验证脚本。

用法（在 backend 目录下，需后端已启动）：
    .venv\\Scripts\\python.exe scripts\\check_keywords.py

为什么用 Python 而不是 PowerShell 测：
    PowerShell 传中文 JSON 时编码不可靠（实测会把"霸凌测试词"写成乱码，
    且单引号里的 ``\\n`` 不会解析为换行），会导致"重复新增未拦截""批量导入全跳过"
    这类**假失败**。用 urllib 显式指定 UTF-8 才能得到可信结论。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(method: str, path: str, data: dict | None = None, token: str | None = None) -> tuple[int, dict]:
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


def main() -> int:
    st, resp = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    if st != 200:
        print("登录失败：", resp)
        return 1
    tok = resp["access_token"]
    print("登录成功\n")

    # ---------- 1. 词表概览 ----------
    _, meta = req("GET", "/api/keywords/meta", token=tok)
    eng = meta["engine"]
    print("【1】词表概览")
    print("    总数 %d 条：报警 %d / 警告 %d / 高亮 %d"
          % (eng["total"], eng["alarm"], eng["warn"], eng["highlight"]))
    print("    分类数：%d 类" % len(meta["categories"]))

    # ---------- 2. 新增（中文）---------- 
    word = "校园方言测试词"
    st, created = req("POST", "/api/keywords",
                      {"word": word, "category": "custom", "level": "warn", "note": "验证脚本"}, token=tok)
    print("\n【2】新增中文词：status=%d word=%s level=%s" % (st, created.get("word"), created.get("level")))
    new_id = created.get("id")

    # ---------- 3. 重复新增应被拦截 ----------
    st, dup = req("POST", "/api/keywords", {"word": word, "category": "custom", "level": "warn"}, token=tok)
    print("【3】重复新增：status=%d（期望 409）detail=%s" % (st, dup.get("detail")))
    ok_dup = st == 409

    # ---------- 4. 批量导入（真实换行）----------
    text = "\n".join(["方言威胁词甲", "方言威胁词乙", "打死你", word])
    st, bulk = req("POST", "/api/keywords/bulk",
                   {"text": text, "category": "custom", "level": "warn"}, token=tok)
    print("【4】批量导入：status=%d 新增 %s 条，跳过 %s 条" % (st, bulk.get("added"), bulk.get("skipped")))
    if bulk.get("detail"):
        print("      详情：%s" % bulk["detail"])
    for d in bulk.get("details", [])[:3]:
        print("      跳过 %s -> %s" % (d["word"], d["reason"]))
    ok_bulk = st == 200 and bool(bulk.get("added"))

    # ---------- 5. 内置词不可删除 ----------
    _, page = req("GET", "/api/keywords?is_preset=true&limit=1", token=tok)
    preset_id = page["items"][0]["id"] if page.get("items") else None
    if preset_id:
        st, r = req("DELETE", f"/api/keywords/{preset_id}", token=tok)
        print("【5】删除内置词：status=%d（期望 400）detail=%s" % (st, r.get("detail")))
        ok_preset = st == 400
    else:
        ok_preset = True
        print("【5】未找到内置词，跳过")

    # ---------- 6. 三档匹配验证 ----------
    print("\n【6】三档匹配验证（直接调用引擎，词表从库中加载）")
    sys.path.insert(0, ".")
    from app.audio.keywords import scan, summarize  # noqa: E402

    cases = [
        ("你再这样我就打死你信不信", "alarm"),
        ("放学别走你等着", "warn"),
        ("他昨天被人推了一下", "highlight"),
        ("这道题我不会做 你教我一下", None),
    ]
    ok_level = True
    for text_c, expect in cases:
        hits = scan(text_c)
        s = summarize(hits)
        got = s["level"]
        flag = "OK " if got == expect else "差异"
        if got != expect:
            ok_level = False
        print("    [%s] %-20s -> 档位=%-9s 命中=%s" % (flag, text_c, got, s["keywords"]))

    # ---------- 7. 清理本次测试数据 ----------
    cleaned = 0
    for w in ["校园方言测试词", "方言威胁词甲", "方言威胁词乙"]:
        st, page = req("GET", f"/api/keywords?keyword={urllib.parse.quote(w)}&limit=5", token=tok)
        for it in page.get("items", []):
            if it["word"] == w and not it["is_preset"]:
                req("DELETE", f"/api/keywords/{it['id']}", token=tok)
                cleaned += 1
    # 清理早期用 PowerShell 测试时因编码失真产生的乱码词（id 为当时返回值）
    for bad_id in (3018, 3019):
        st, _ = req("DELETE", f"/api/keywords/{bad_id}", token=tok)
        if st == 200:
            cleaned += 1
    print("\n【7】已清理测试词 %d 条" % cleaned)

    print("\n结论：重复拦截 %s / 内置词保护 %s / 批量导入 %s / 三档判定 %s"
          % ("通过" if ok_dup else "失败",
             "通过" if ok_preset else "失败",
             "通过" if ok_bulk else "失败",
             "通过" if ok_level else "有差异"))
    return 0 if (ok_dup and ok_preset and ok_bulk and ok_level) else 2


if __name__ == "__main__":
    raise SystemExit(main())
