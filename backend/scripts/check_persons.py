"""人员档案 + 人脸建档 + 合规 —— 可复现验证脚本。

用法（在 backend 目录下，需后端已启动）：
    .venv\\Scripts\\python.exe scripts\\check_persons.py

验证内容：
  1. 班级与三类人员（学生/教师/管理人员）的增删改查
  2. 筛选（关键字 / 班级 / 状态 / 同意状态 / 人脸建档状态）
  3. CSV 批量导入（含跳过行原因）
  4. 人脸处理同意的登记与撤回（撤回必须连带删除特征）
  5. 人脸能力状态与 1:N 检索接口的降级行为
  6. 合规自查看板与审计留痕

脚本会清理自己创建的数据，不影响现场真实档案。
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

# 允许用环境变量指向非默认端口：本机上常有其他项目同时占用 8000，
# 脚本不应因此无法验证
BASE = os.environ.get("CAB_CHECK_BASE", "http://127.0.0.1:8000").rstrip("/")
TAG = uuid.uuid4().hex[:6]          # 保证多次运行不冲突，也便于精确清理


def req(method: str, path: str, data=None, token: str | None = None, raw: bytes | None = None,
        ctype: str = "application/json; charset=utf-8"):
    body = raw if raw is not None else (
        json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None)
    r = urllib.request.Request(BASE + path, data=body, method=method)
    r.add_header("Content-Type", ctype)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"detail": text}


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """构造 multipart/form-data 请求体（不引入额外依赖）。"""
    boundary = "----cab" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        out.write(f"{v}\r\n".encode())
    out.write(f"--{boundary}\r\n".encode())
    out.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    out.write(b"Content-Type: application/octet-stream\r\n\r\n")
    out.write(content)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def blank_jpeg() -> bytes:
    """一张纯色图：必然检测不到人脸，用于验证"无脸时优雅拒绝"。"""
    import cv2
    import numpy as np

    img = np.full((480, 640, 3), 200, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else b""


def main() -> int:
    st, resp = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    if st != 200:
        print("登录失败：", resp)
        return 1
    tok = resp["access_token"]
    print("登录成功，测试标记 =", TAG, "\n")

    created: list[tuple[str, int]] = []
    class_id = None
    ok_all = True

    try:
        # ---------- 1. 班级 ----------
        st, cls = req("POST", "/api/classes", {"name": f"测试班-{TAG}", "grade": "测试年级"}, tok)
        print("【1】创建班级：status=%d name=%s" % (st, cls.get("name")))
        class_id = cls.get("id")
        ok_cls = st == 201 and class_id

        # ---------- 2. 三类人员 ----------
        print("\n【2】新增三类人员")
        samples = {
            "student": {"no": f"S{TAG}", "name": "测试学生", "gender": "男", "class_id": class_id,
                        "enroll_year": 2024, "guardian_name": "测试家长",
                        "guardian_phone": "13800000000", "guardian_relation": "父"},
            "teacher": {"no": f"T{TAG}", "name": "测试教师", "gender": "女",
                        "department": "测试教研组", "title": "一级教师", "subject": "数学"},
            "staff": {"no": f"A{TAG}", "name": "测试职工", "gender": "男",
                      "department": "测试处室", "position": "主任"},
        }
        for owner_type, payload in samples.items():
            st, person = req("POST", "/api/persons", {"owner_type": owner_type, **payload}, tok)
            ok = st == 201
            ok_all &= bool(ok)
            if ok:
                created.append((owner_type, person["id"]))
            print("      %-8s status=%d id=%s 姓名=%s" % (
                owner_type, st, person.get("id"), person.get("name")))

        # 重复编号应被拒
        st, dup = req("POST", "/api/persons", {"owner_type": "student", **samples["student"]}, tok)
        print("      重复编号拦截：status=%d detail=%s" % (st, dup.get("detail")))
        ok_all &= st == 400

        # ---------- 3. 列表与筛选 ----------
        print("\n【3】列表与筛选")
        st, page = req("GET", f"/api/persons?owner_type=student&q={TAG}", token=tok)
        print("      关键字搜索：命中 %d 条" % page.get("total", 0))
        ok_all &= page.get("total", 0) >= 1

        st, page = req("GET", f"/api/persons?owner_type=student&class_id={class_id}", token=tok)
        print("      按班级筛选：命中 %d 条" % page.get("total", 0))
        ok_all &= page.get("total", 0) >= 1

        st, page = req("GET", "/api/persons?owner_type=student&has_face=false&limit=5", token=tok)
        print("      未建档筛选：命中 %d 条（不应报错）" % page.get("total", 0))
        ok_all &= st == 200

        st, detail = req("GET", f"/api/persons/student/{created[0][1]}", token=tok)
        print("      详情：%s 班级=%s 同意=%s" % (
            detail.get("name"), detail.get("class_name"), detail["face"]["consent_label"]))
        ok_all &= detail.get("class_name") == cls.get("name")

        # ---------- 4. CSV 导入 ----------
        print("\n【4】CSV 批量导入")
        csv_text = (
            "学号,姓名,性别,班级,监护人\n"
            f"C{TAG}01,批量甲,男,测试班-{TAG},甲家长\n"
            f"C{TAG}02,批量乙,女,测试班-{TAG},乙家长\n"
            ",缺编号的行,男,,\n"
        )
        body, ctype = multipart({}, "students.csv", csv_text.encode("utf-8-sig"))
        st, res = req("POST", "/api/persons/import?owner_type=student", token=tok,
                      raw=body, ctype=ctype)
        print("      status=%d 新增=%s 更新=%s 跳过=%s 原因=%s" % (
            st, res.get("created"), res.get("updated"), res.get("skipped_count"),
            [s.get("reason") for s in res.get("skipped", [])]))
        ok_all &= st == 200 and res.get("created") == 2 and res.get("skipped_count") == 1
        if st == 200:
            st2, page2 = req("GET", f"/api/persons?owner_type=student&q=C{TAG}", token=tok)
            for it in page2.get("items", []):
                created.append(("student", it["id"]))

        # ---------- 5. 同意登记与撤回 ----------
        print("\n【5】人脸处理同意")
        sid = created[0][1]
        st, person = req("POST", f"/api/persons/student/{sid}/consent",
                         {"granted": True, "subject": "guardian", "method": "written"}, tok)
        print("      登记同意：status=%d 状态=%s 主体=%s" % (
            st, person["face"]["consent_label"], person["face"]["consent_subject_label"]))
        ok_all &= st == 200 and person["face"]["consent_status"] == "granted"

        st, person = req("POST", f"/api/persons/student/{sid}/consent",
                         {"granted": False, "subject": "guardian", "method": "electronic"}, tok)
        print("      撤回同意：status=%d 状态=%s（应同时清空人脸）" % (
            st, person["face"]["consent_label"]))
        ok_all &= st == 200 and person["face"]["consent_status"] == "revoked" \
            and person["face"]["template_count"] == 0

        st, records = req("GET", f"/api/persons/student/{sid}/consents", token=tok)
        print("      同意留痕：%d 条 %s" % (len(records), [r["action_label"] for r in records]))
        ok_all &= len(records) >= 2

        # ---------- 6. 人脸能力 ----------
        print("\n【6】人脸能力与降级行为")
        st, fs = req("GET", "/api/faces/status", token=tok)
        print("      检测器：backend=%s landmarks=%s ready=%s" % (
            fs["detector"]["backend"], fs["detector"]["landmarks"], fs["detector"]["ready"]))
        print("      识别器：backend=%s ready=%s dim=%s error=%s" % (
            fs["identity"]["backend"], fs["identity"]["ready"], fs["identity"]["dim"],
            fs["identity"]["error"] or "-"))
        print("      索引：persons=%s templates=%s ready=%s" % (
            fs["index"]["persons"], fs["index"]["templates"], fs["index"]["ready"]))
        ok_all &= fs["detector"]["ready"] is True

        img = blank_jpeg()
        body, ctype = multipart({}, "blank.jpg", img)
        st, res = req("POST", "/api/faces/identify", token=tok, raw=body, ctype=ctype)
        print("      无脸图检索：ok=%s 原因=%s（应为「未检测到人脸」）" % (
            res.get("ok"), (res.get("reasons") or ["-"])[0]))
        ok_all &= res.get("ok") is False

        body, ctype = multipart(
            {"owner_type": "student", "person_id": str(sid), "consent_subject": "guardian"},
            "blank.jpg", img)
        st, res = req("POST", "/api/faces/enroll", token=tok, raw=body, ctype=ctype)
        print("      无脸图建档：ok=%s 原因=%s" % (res.get("ok"), (res.get("reasons") or ["-"])[0]))
        ok_all &= res.get("ok") is False

        # ---------- 7. 合规 ----------
        print("\n【7】合规自查看板")
        st, ov = req("GET", "/api/compliance/overview", token=tok)
        print("      人员：%s / 同意：%s / 模板：%s" % (
            ov["people"], ov["consent"], ov["face"]))
        print("      PIA：%s" % ov["pia"])
        print("      检查清单 %d 项，其中已完成 %d 项" % (
            len(ov["checklist"]), sum(1 for c in ov["checklist"] if c["done"] is True)))
        ok_all &= st == 200 and len(ov["checklist"]) >= 6

        st, notice = req("GET", "/api/compliance/notice", token=tok)
        print("      告知同意书：%d 字，含监护人条款=%s" % (
            len(notice["content"]), "监护人" in notice["content"]))
        ok_all &= "监护人" in notice["content"]

        st, audit = req("GET", "/api/compliance/audit?limit=10", token=tok)
        actions = [a["action"] for a in audit["items"]]
        print("      审计留痕：最近 %d 条 %s" % (len(actions), actions[:6]))
        ok_all &= any(a.startswith("consent") for a in actions) or any(a.startswith("person") for a in actions)

    finally:
        # ---------- 清理 ----------
        print("\n【清理】")
        removed = 0
        for owner_type, pid in created:
            st, _ = req("DELETE", f"/api/persons/{owner_type}/{pid}", token=tok)
            if st == 200:
                removed += 1
        # CSV 导入的行单独按编号清理
        for suffix in ("C%s01" % TAG, "C%s02" % TAG):
            st, page = req("GET", f"/api/persons?owner_type=student&q={suffix}", token=tok)
            for it in page.get("items", []):
                if it["no"] == suffix:
                    req("DELETE", f"/api/persons/student/{it['id']}", token=tok)
                    removed += 1
        print("      已删除人员 %d 条" % removed)
        # 班级要单独删：非空班级会被后端拒绝，所以必须放在人员清理之后
        if class_id:
            st, _ = req("DELETE", f"/api/classes/{class_id}", token=tok)
            print("      已删除测试班级：status=%d" % st)

    print("\n结论：%s" % ("全部通过" if ok_all else "存在失败项，请查看上方输出"))
    return 0 if ok_all else 2


if __name__ == "__main__":
    sys.exit(main())
