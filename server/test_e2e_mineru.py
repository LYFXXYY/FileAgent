"""端到端集成验证：扫描件 PDF → MinerU OCR 兜底 → 批改 → 下载，零真实费用。

需要两个假端点进程（两个终端）：
  1) cd server && uv run uvicorn test_llm_server:app --port 19000
  2) cd server && uv run uvicorn test_mineru_server:app --port 19002
然后：
  3) cd server && uv run python test_e2e_mineru.py

与另外三套 e2e 不同：**后端由本脚本自己拉起**（FA_PORT=8010），因为要注入
MINERU_TOKEN / MINERU_BASE_URL 指向假 MinerU——日常 .env 里 token 留空，
不能也不该为了跑测试去改它。脚本结束时后端子进程一并停掉。
⚠ 仍走 run_backend.py 入口（Windows Selector policy，见 AGENTS.md §2）。

验证点：
- 无文字层 / 薄文字层两条扫描件判据都被受理（ocr_pending=True），不再 400
- OCR 在处理阶段跑：plain_text 写回后批改定位照常（start/end 非空）
- 表格 / 图片不进批改正文；structured 三件套按 log_id 落盘
- 文字型 PDF 不受影响、不触发 MinerU
- MinerU failed → 日志 failed 且文案可行动，不产生 llm_calls
- MinerU 不写 llm_calls（按页计费无 token，同 embedding 边界）
- md 转换任务：md+批改互斥（创建 422 / 更新 400）；docx 本地转换零外呼；
  PDF（含文字型）走 MinerU，带图交付 zip（md + images/），全程零 llm_calls

跑完自动清理：DB 记录、outputs/mineru/{log_id}/、uploads 里本次的落盘文件
（stored_filename 列让落盘副本首次可精确回溯）。
"""

import io
import os
import shutil
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import pymupdf

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FA_MINERU_E2E_PORT", "8010"))
API = f"http://127.0.0.1:{PORT}/api/v1"
LLM = os.environ.get("FA_MOCK_LLM", "http://127.0.0.1:19000/v1")
MINERU = os.environ.get("FA_MOCK_MINERU", "http://127.0.0.1:19002")
MOCK_TOKEN = "e2e-mineru-token"

PASS = FAIL = 0


def check(name: str, condition: bool, extra: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _pixmap() -> pymupdf.Pixmap:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 48, 48))
    pix.set_rect(pix.irect, (120, 80, 200))
    return pix


def scanned_pdf_no_layer(pages: int = 2) -> bytes:
    """纯图片页、零文字层：触发第一条扫描件判据"""
    doc = pymupdf.open()
    pix = _pixmap()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(72, 72, 500, 700), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def scanned_pdf_thin_layer(pages: int = 3) -> bytes:
    """每页一行图注 + 一张图：触发薄文字层判据（图注逐页不同，躲开噪声判据）"""
    doc = pymupdf.open()
    pix = _pixmap()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 300), f"图{i + 1}说明", fontname="china-s", fontsize=11)
        page.insert_image(pymupdf.Rect(72, 320, 120, 368), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def text_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "这是一篇文字型 PDF。", fontname="china-s", fontsize=11)
    page.insert_text((72, 92), "他高兴的跳了起来。", fontname="china-s", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def sample_docx() -> bytes:
    """md 转换的 Word 样本：标题 + 正文，本地转换零费用"""
    from docx import Document

    doc = Document()
    doc.add_heading("转换样本标题", level=1)
    doc.add_paragraph("这是 Word 正文段落。")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def start_backend() -> subprocess.Popen:
    env = {
        **os.environ,
        "FA_PORT": str(PORT),
        "MINERU_TOKEN": MOCK_TOKEN,
        "MINERU_BASE_URL": MINERU,
        "MINERU_POLL_INTERVAL": "0.2",
        "MINERU_MAX_WAIT": "30",
        "LLM_TRUST_ENV_PROXY": "false",
        # MinerU client 是 trust_env=True（真实端点在境外要能走代理），
        # 但本机 NO_PROXY 含裸 [::1] 会让 httpx 抛 InvalidURL——e2e 里覆盖成干净值
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    log = open(os.path.join(SERVER_DIR, "_e2e_mineru_backend.log"), "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "run_backend.py"], cwd=SERVER_DIR, env=env, stdout=log, stderr=log
    )


def wait_backend() -> bool:
    for _ in range(40):
        try:
            if (
                httpx.get(
                    f"http://127.0.0.1:{PORT}/api/health", timeout=3, trust_env=False
                ).status_code
                == 200
            ):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def poll_final(c: httpx.Client, tok: dict, log_id: int, want: str) -> dict:
    final = {}
    for _ in range(60):
        time.sleep(0.5)
        try:
            final = c.get(f"{API}/process/{log_id}/status", headers=tok).json()
        except httpx.HTTPError:
            continue
        if final.get("status") == want:
            return final
        if final.get("status") in ("done", "failed"):
            return final
    return final


def upload(c: httpx.Client, tok: dict, task_id: int, name: str, data: bytes) -> httpx.Response:
    return c.post(
        f"{API}/upload?task_id={task_id}",
        headers=tok,
        files={"file": (name, data, "application/pdf")},
    )


def cleanup(user_id, provider_id, task_ids, log_ids):
    import database
    from models.llm_call import LlmCall
    from models.operation_log import OperationLog
    from models.provider import ModelProvider
    from models.task import Task
    from models.user import User

    stored_names = []
    with database.SessionLocal() as db:
        for lid in log_ids:
            log = db.query(OperationLog).filter(OperationLog.id == lid).first()
            if log is not None and log.stored_filename:
                stored_names.append(log.stored_filename)
        if user_id:
            db.query(LlmCall).filter(LlmCall.user_id == user_id).delete()
        for lid in log_ids:
            db.query(OperationLog).filter(OperationLog.id == lid).delete()
        for tid in task_ids:
            db.query(Task).filter(Task.id == tid).delete()
        if provider_id:
            db.query(ModelProvider).filter(ModelProvider.id == provider_id).delete()
        if user_id:
            db.query(User).filter(User.id == user_id).delete()
        db.commit()

    for lid in log_ids:
        shutil.rmtree(os.path.join(SERVER_DIR, "outputs", "mineru", str(lid)), ignore_errors=True)
    for name in stored_names:
        try:
            os.remove(os.path.join(SERVER_DIR, "uploads", name))
        except OSError:
            pass


def main():
    try:
        httpx.get(f"{LLM}/models", timeout=2, trust_env=False)
    except httpx.HTTPError:
        print(f"假 LLM 端点不在 {LLM}，请先 uv run uvicorn test_llm_server:app --port 19000")
        sys.exit(3)
    try:
        httpx.post(f"{MINERU}/_mock/reset", timeout=2, trust_env=False)
    except httpx.HTTPError:
        print(
            f"假 MinerU 端点不在 {MINERU}，请先 uv run uvicorn test_mineru_server:app --port 19002"
        )
        sys.exit(3)

    proc = start_backend()
    if not wait_backend():
        print(f"后端没起来（{PORT}），看 server/_e2e_mineru_backend.log")
        proc.terminate()
        sys.exit(2)

    suffix = str(int(time.time()))[-6:]
    c = httpx.Client(trust_env=False, timeout=30.0)
    user = f"e2e_m_{suffix}"
    tok: dict[str, str] = {}
    user_id = provider_id = task_id = md_task_id = None
    log_ids: list[int] = []

    try:
        print("\n===== 1. 注册 + 登录 =====")
        r = c.post(f"{API}/auth/register", json={"username": user, "password": "secret123"})
        check("注册 201", r.status_code == 201, r.text)
        user_id = r.json().get("id") if r.status_code == 201 else None
        r = c.post(f"{API}/auth/login", json={"username": user, "password": "secret123"})
        check("登录 200", r.status_code == 200, r.text)
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n===== 2. 假 LLM 提供商 + 批改任务 =====")
        r = c.post(
            f"{API}/providers",
            headers=tok,
            json={"name": "mock-text", "base_url": LLM, "api_key": "sk-mock", "model_name": "mock"},
        )
        check("POST provider 201", r.status_code == 201, r.text)
        provider_id = r.json()["id"]
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "扫描件批改",
                "description": "",
                "prompt_template": "请批改作文：\n\n{{content}}",
                "is_grading": True,
                "provider_id": provider_id,
            },
        )
        check("POST task 201", r.status_code == 201, r.text)
        task_id = r.json()["id"]

        print("\n===== 3. 上传无文字层扫描件：受理而不是 400 =====")
        pdf_bytes = scanned_pdf_no_layer()
        r = upload(c, tok, task_id, "扫描件.pdf", pdf_bytes)
        check("上传 201", r.status_code == 201, r.text)
        body = r.json()
        log_id = body["log_id"]
        log_ids.append(log_id)
        check("ocr_pending=True", body.get("ocr_pending") is True, r.text)
        check("extracted_text 为空是预期", body.get("extracted_text") == "", r.text)
        check("content_length=0", body.get("content_length") == 0, r.text)

        print("\n===== 4. 处理：OCR 写回 → 批改 → 定位照常 =====")
        r = c.post(f"{API}/process/{log_id}", headers=tok)
        check("POST /process 200", r.status_code == 200, r.text)
        check(
            "返回 processing + ocr_pending",
            r.json().get("status") == "processing" and r.json().get("ocr_pending") is True,
            r.text,
        )
        final = poll_final(c, tok, log_id, "done")
        check("轮询到 done", final.get("status") == "done", str(final)[:300])
        check("processed_content 含修订", "修订" in (final.get("processed_content") or ""))
        check("grading_degraded 未触发", final.get("grading_degraded") is False)
        issues = final.get("issues") or []
        check("issues 恰 1 条", len(issues) == 1, str(issues)[:200])
        if issues:
            check("OCR 原文上定位成功 start", issues[0].get("start") is not None)
            check("OCR 原文上定位成功 end", issues[0].get("end") is not None)
        original = final.get("original_content") or ""
        check("status 回填 OCR 原文", "他高兴的跳了起来" in original, original[:100])
        check("表格不进正文", "|" not in original, original[:200])
        check("图片引用不进正文", "![" not in original, original[:200])

        print("\n===== 5. 假 MinerU 收到的请求形状 =====")
        last = c.get(f"{MINERU}/_mock/last", timeout=5).json().get("batch") or {}
        check("上传字节与原文件一致", last.get("uploaded_len") == len(pdf_bytes), str(last)[:200])
        files = (last.get("request") or {}).get("files") or [{}]
        check("is_ocr=True", files[0].get("is_ocr") is True, str(files))
        check(
            "enable_table=True（structured 要完整）",
            (last.get("request") or {}).get("enable_table") is True,
        )
        check(
            "PUT 不带 Content-Type",
            last.get("put_content_type") is None,
            str(last.get("put_content_type")),
        )

        print("\n===== 6. structured 三件套按 log_id 落盘 =====")
        base = os.path.join(SERVER_DIR, "outputs", "mineru", str(log_id))
        check("full.md 落盘", os.path.isfile(os.path.join(base, "full.md")))
        check("content_list.json 落盘", os.path.isfile(os.path.join(base, "content_list.json")))
        check("images/a.jpg 落盘", os.path.isfile(os.path.join(base, "images", "a.jpg")))

        print("\n===== 7. 下载 diff 不受 OCR 来源影响 =====")
        from docx import Document

        r = c.get(f"{API}/download/{log_id}", headers=tok)
        check("下载 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            text = "\n".join(p.text for p in Document(io.BytesIO(r.content)).paragraphs)
            check("docx 含修订文", "高兴地跳了起来" in text, text[:200])
            check("docx 含 OCR 原文", "他高兴的跳了起来" in text, text[:200])

        print("\n===== 8. 薄文字层扫描件同样受理 =====")
        r = upload(c, tok, task_id, "薄层扫描件.pdf", scanned_pdf_thin_layer())
        check(
            "上传 201 + ocr_pending",
            r.status_code == 201 and r.json().get("ocr_pending") is True,
            r.text,
        )
        log2 = r.json()["log_id"]
        log_ids.append(log2)
        c.post(f"{API}/process/{log2}", headers=tok)
        final2 = poll_final(c, tok, log2, "done")
        check("薄层件也 done", final2.get("status") == "done", str(final2)[:200])
        batch_after_thin = (c.get(f"{MINERU}/_mock/last", timeout=5).json().get("batch") or {}).get(
            "batch_id"
        )

        print("\n===== 9. 文字型 PDF 不受影响、不触发 MinerU =====")
        r = upload(c, tok, task_id, "文字型.pdf", text_pdf())
        check(
            "文字型 ocr_pending=False",
            r.status_code == 201 and r.json().get("ocr_pending") is False,
            r.text,
        )
        check("文字型照常提取", "高兴" in r.json().get("extracted_text", ""), r.text)
        log3 = r.json()["log_id"]
        log_ids.append(log3)
        c.post(f"{API}/process/{log3}", headers=tok)
        final3 = poll_final(c, tok, log3, "done")
        check("文字型 done", final3.get("status") == "done", str(final3)[:200])
        batch_now = (c.get(f"{MINERU}/_mock/last", timeout=5).json().get("batch") or {}).get(
            "batch_id"
        )
        check(
            "没有新的 MinerU batch",
            batch_now == batch_after_thin,
            f"{batch_after_thin} → {batch_now}",
        )

        print("\n===== 10. MinerU failed → 日志 failed，文案可行动 =====")
        c.post(f"{MINERU}/_mock/mode", json={"mode": "failed"}, timeout=5)
        r = upload(c, tok, task_id, "失败扫描件.pdf", scanned_pdf_no_layer())
        log4 = r.json()["log_id"]
        log_ids.append(log4)
        c.post(f"{API}/process/{log4}", headers=tok)
        final4 = poll_final(c, tok, log4, "failed")
        check("轮询到 failed", final4.get("status") == "failed", str(final4)[:200])
        err = final4.get("error_message") or ""
        check("文案含扫描件识别失败", "扫描件识别失败" in err, err)
        check("文案带上游原因", "mock 解析失败" in err, err)
        r = c.get(f"{API}/download/{log4}", headers=tok)
        check("失败后下载 400", r.status_code == 400, str(r.status_code))
        c.post(f"{MINERU}/_mock/mode", json={"mode": "done"}, timeout=5)

        print("\n===== 11. 计量边界：MinerU 不写 llm_calls =====")
        import database
        from models.llm_call import LlmCall

        with database.SessionLocal() as db:
            rows = db.query(LlmCall).filter(LlmCall.user_id == user_id).all()
            purposes = {row.purpose for row in rows}
            check("只有批改类计量", purposes <= {"grading", "grading_degraded"}, str(purposes))
            ocr_failed_calls = [row for row in rows if row.log_id == log4]
            check(
                "OCR 失败的日志零外呼计量", len(ocr_failed_calls) == 0, str(len(ocr_failed_calls))
            )

        print("\n===== 12. md 任务：创建互斥 + 更新互斥 =====")
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "非法md批改",
                "prompt_template": "{{content}}",
                "is_grading": True,
                "output_format": "md",
            },
        )
        check("md+批改创建被拦 422", r.status_code == 422, r.text[:200])
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "文档转Markdown",
                "prompt_template": "{{content}}",
                "is_grading": False,
                "output_format": "md",
            },
        )
        check("md 任务创建 201", r.status_code == 201, r.text)
        md_task_id = r.json()["id"]
        r = c.put(f"{API}/tasks/{md_task_id}", headers=tok, json={"is_grading": True})
        check("md 任务改批改被拦 400", r.status_code == 400, r.text[:200])

        print("\n===== 13. docx → md：本地转换，零 MinerU 零 LLM =====")
        batch_before_md = (c.get(f"{MINERU}/_mock/last", timeout=5).json().get("batch") or {}).get(
            "batch_id"
        )
        docx_bytes = sample_docx()
        r = upload(c, tok, md_task_id, "转换样本.docx", docx_bytes)
        check("docx 上传 201", r.status_code == 201, r.text)
        log5 = r.json()["log_id"]
        log_ids.append(log5)
        check("docx 无 OCR 标记", r.json().get("ocr_pending") is False, r.text)
        c.post(f"{API}/process/{log5}", headers=tok)
        final5 = poll_final(c, tok, log5, "done")
        check("docx 转换 done", final5.get("status") == "done", str(final5)[:200])
        md5 = final5.get("processed_content") or ""
        check("产物是 markdown 标题", "# 转换样本标题" in md5, md5[:200])
        check("正文保留", "这是 Word 正文段落。" in md5, md5[:200])
        batch_after_docx_md = (
            c.get(f"{MINERU}/_mock/last", timeout=5).json().get("batch") or {}
        ).get("batch_id")
        check("docx 路径不触发 MinerU", batch_after_docx_md == batch_before_md)
        r = c.get(f"{API}/download/{log5}", headers=tok)
        check("md 下载 200", r.status_code == 200, str(r.status_code))
        ctype = r.headers.get("content-type", "")
        disp = r.headers.get("content-disposition", "")
        check("裸 md 媒体类型", ctype.startswith("text/markdown"), ctype)
        check("下载名 .md 且带转换前缀", ".md" in disp and "%E8%BD%AC%E6%8D%A2" in disp, disp)
        check("下载内容即 markdown", r.content.decode("utf-8") == md5)

        print("\n===== 14. PDF → md：文字型也走 MinerU，带图打 zip =====")
        r = upload(c, tok, md_task_id, "md样本.pdf", text_pdf())
        check("pdf 上传 201", r.status_code == 201, r.text)
        log6 = r.json()["log_id"]
        log_ids.append(log6)
        c.post(f"{API}/process/{log6}", headers=tok)
        final6 = poll_final(c, tok, log6, "done")
        check("pdf 转换 done", final6.get("status") == "done", str(final6)[:200])
        md6 = final6.get("processed_content") or ""
        check("产物含 MinerU markdown 标题", "# 扫描件标题" in md6, md6[:200])
        check("图片引用保留在 md", "![插图](images/a.jpg)" in md6, md6[:300])
        orig6 = final6.get("original_content") or ""
        check("纯文本回填不含图片引用", "![" not in orig6 and "高兴" in orig6, orig6[:200])
        batch_after_pdf_md = (
            c.get(f"{MINERU}/_mock/last", timeout=5).json().get("batch") or {}
        ).get("batch_id")
        check("文字型 PDF 转 md 也触发 MinerU", batch_after_pdf_md != batch_after_docx_md)
        base6 = os.path.join(SERVER_DIR, "outputs", "mineru", str(log6))
        check("md 路径 structured 落盘", os.path.isfile(os.path.join(base6, "full.md")))
        r = c.get(f"{API}/download/{log6}", headers=tok)
        check("带图下载 200", r.status_code == 200, str(r.status_code))
        check("zip 媒体类型", r.headers.get("content-type") == "application/zip")
        check("下载名 .zip", ".zip" in r.headers.get("content-disposition", ""))
        import zipfile

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = set(zf.namelist())
            check("zip 布局 md+images", names == {"md样本.md", "images/a.jpg"}, str(names))
            check("zip 内 md 与产物一致", zf.read("md样本.md").decode("utf-8") == md6)

        print("\n===== 15. md 全程零 llm_calls =====")
        with database.SessionLocal() as db:
            md_calls = (
                db.query(LlmCall)
                .filter(LlmCall.user_id == user_id, LlmCall.log_id.in_([log5, log6]))
                .count()
            )
            check("md 两条日志零计量", md_calls == 0, str(md_calls))
    finally:
        c.close()
        cleanup(
            user_id,
            provider_id,
            [t for t in (task_id, md_task_id) if t is not None],
            log_ids,
        )
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            if FAIL == 0:
                os.remove(os.path.join(SERVER_DIR, "_e2e_mineru_backend.log"))
        except OSError:
            pass

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
