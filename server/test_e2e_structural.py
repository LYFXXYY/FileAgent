"""端到端集成验证：假 OpenAI 兼容端点 + 真实后端 + 真实 MySQL。

需要两个进程（两个终端）：
  1) cd server && uv run uvicorn test_llm_server:app --port 19000
  2) cd server && uv run uvicorn main:app --port 8000   （或复用已启动的）
  3) cd server && uv run python test_e2e_structural.py

验证的是「AI 模型 → 结构化解析 → 定位 → 入库 → 下载 diff」全链路，
外加 docx / PDF 两种输入的提取同构，不依赖任何真实 API Key、不产生 token 费用。

默认端口 8000 / 19000。被占用时用 FA_API 与 FA_MOCK_LLM 同时覆盖，例如：
  FA_API=http://127.0.0.1:8081/api/v1 FA_MOCK_LLM=http://127.0.0.1:19001/v1 \\
    uv run python test_e2e_structural.py

它会注册 e2e_s_* 账号并造日志、落盘文件与 llm_calls 行，跑完要手动清（见 AGENTS.md §7）。
"""

import io
import os
import sys
import time
from urllib.parse import unquote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import pymupdf
from docx import Document

API = os.environ.get("FA_API", "http://127.0.0.1:8000/api/v1")
# 假端点地址。改端口时两个环境变量要一起设
LLM = os.environ.get("FA_MOCK_LLM", "http://127.0.0.1:19000/v1")

PASS = FAIL = 0


def check(name: str, condition: bool, extra: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def make_pdf(lines: list[str]) -> bytes:
    """
    造一份每视觉行各占一个文本块的 PDF —— 正是最难处理的那种（幻灯导出件常见形态）

    中文要用内置 china-s 字体，默认字体写中文会变成一串间隔号。
    """
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for text in lines:
        page.insert_text((72, y), text, fontname="china-s", fontsize=11)
        y += 17.0
    data = doc.tobytes()
    doc.close()
    return data


def wait_backend() -> bool:
    """health 端点在 API 的上一级，从 FA_API 反推而不是另写一遍端口"""
    health_url = API.rsplit("/api/", 1)[0] + "/api/health"
    for _ in range(15):
        try:
            if httpx.get(health_url, timeout=3, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def main():
    if not wait_backend():
        print(f"后端不在 {API.rsplit('/api/', 1)[0]}")
        sys.exit(2)
    try:
        httpx.get(f"{LLM}/models", timeout=2, trust_env=False)
    except httpx.HTTPError:
        print(f"假 LLM 端点不在 {LLM}，请先 uv run uvicorn test_llm_server:app --port 19000")
        sys.exit(3)

    suffix = str(int(time.time()))[-6:]
    c = httpx.Client(trust_env=False, timeout=30.0)
    user = f"e2e_s_{suffix}"
    tok: dict[str, str] = {}

    print("\n===== 1. 注册 + 登录 =====")
    r = c.post(f"{API}/auth/register", json={"username": user, "password": "secret123"})
    check("注册 201", r.status_code == 201, r.text)
    r = c.post(f"{API}/auth/login", json={"username": user, "password": "secret123"})
    check("登录 200", r.status_code == 200, r.text)
    tok = {"Authorization": f"Bearer {r.json()['access_token']}"}

    print("\n===== 2. 假 LLM 提供商（能力 text） =====")
    r = c.post(
        f"{API}/providers",
        headers=tok,
        json={
            "name": "mock-text",
            "base_url": LLM,
            "api_key": "sk-mock",
            "model_name": "mock",
        },
    )
    check("POST provider 201", r.status_code == 201, r.text)
    check("capabilities 默认 text", r.json().get("capabilities") == "text", str(r.json()))
    provider_id = r.json()["id"]

    print("\n===== 3. 批改任务 is_grading =====")
    r = c.post(
        f"{API}/tasks",
        headers=tok,
        json={
            "name": "的地不分",
            "description": "",
            "prompt_template": "请批改作文：\n\n{{content}}",
            "is_grading": True,
            "provider_id": provider_id,
        },
    )
    check("POST task 201", r.status_code == 201, r.text)
    task_id = r.json()["id"]

    print("\n===== 4. 上传含错别字的 docx =====")
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("这是一篇测试作文。")
    doc.add_paragraph("他高兴的跳了起来。")  # 的/地 错
    doc.save(buf)
    r = c.post(
        f"{API}/upload?task_id={task_id}",
        headers=tok,
        files={
            "file": (
                "e2e.docx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    check("上传 201", r.status_code == 201, r.text)
    log_id = r.json()["log_id"]
    docx_extracted = r.json()["extracted_text"]
    check("原文提取含高兴", "高兴" in docx_extracted, docx_extracted)

    print("\n===== 5. 触发处理（后台） =====")
    r = c.post(f"{API}/process/{log_id}", headers=tok)
    check("POST /process 200", r.status_code == 200, r.text)
    check("返回 processing", r.json().get("status") == "processing", r.text)

    final = {}
    for _ in range(30):
        time.sleep(1)
        final = c.get(f"{API}/process/{log_id}/status", headers=tok).json()
        if final.get("status") in ("done", "failed"):
            break

    print("\n===== 6. 结构化验证 =====")
    check("轮询到 done", final.get("status") == "done", str(final))
    check("processed_content 含修订", "修订" in (final.get("processed_content") or ""))
    check("grading_degraded 未触发", final.get("grading_degraded") is False)

    issues = final.get("issues")
    check("issues 非空", isinstance(issues, list) and len(issues) == 1, str(issues))
    if issues:
        iss = issues[0]
        check("type 归一化正确", iss.get("type") == "typo")
        check("original 是片段", iss.get("original") == "高兴的跳了起来")
        check("suggestion 正确", iss.get("suggestion") == "高兴地跳了起来")
        check("confidence 正确", iss.get("confidence") == 0.92)
        check("后端算出定位", iss.get("start") is not None)
        check("end 正确", iss.get("end") is not None)

        # 列表页只带条数，不带整份清单
        r = c.get(f"{API}/logs", headers=tok)
        for item in r.json().get("items", []):
            if item["id"] == log_id:
                check("日志有 issue_count", item.get("issue_count") == 1, str(item))
                break

    print("\n===== 7. 下载 diff 不受结构化影响 =====")
    r = c.get(f"{API}/download/{log_id}", headers=tok)
    check("下载 200", r.status_code == 200, str(r.status_code))
    check("Content-Type 是 docx", "officedocument" in r.headers.get("content-type", ""))
    if r.status_code == 200:
        parsed = Document(io.BytesIO(r.content))
        text = "\n".join(p.text for p in parsed.paragraphs)
        check("docx 含修订文", "高兴地跳了起来" in text, text[:200])
        check("docx 含原文（diff 未丢字）", "他高兴的跳了起来" in text, text[:200])

    print("\n===== 8. PDF 输入：提取同构、定位照旧、结果名换成 .docx =====")
    r = c.post(
        f"{API}/upload?task_id={task_id}",
        headers=tok,
        files={
            "file": (
                "e2e.pdf",
                make_pdf(["这是一篇测试作文。", "他高兴的跳了起来。"]),
                "application/pdf",
            )
        },
    )
    check("PDF 上传 201", r.status_code == 201, r.text)
    pdf_log = r.json()["log_id"]
    pdf_extracted = r.json()["extracted_text"]
    # 两种来源必须吐出完全一样的段落结构，否则 diff 与定位要为 PDF 单独写一套
    check("PDF 提取与 docx 同构", pdf_extracted == docx_extracted, repr(pdf_extracted))

    c.post(f"{API}/process/{pdf_log}", headers=tok)
    pdf_final = {}
    for _ in range(30):
        time.sleep(1)
        pdf_final = c.get(f"{API}/process/{pdf_log}/status", headers=tok).json()
        if pdf_final.get("status") in ("done", "failed"):
            break
    check("PDF 轮询到 done", pdf_final.get("status") == "done", str(pdf_final))
    pdf_issues = pdf_final.get("issues") or []
    check(
        "PDF 也拿到定位",
        bool(pdf_issues) and pdf_issues[0].get("start") is not None,
        str(pdf_issues),
    )

    r = c.get(f"{API}/download/{pdf_log}", headers=tok)
    disposition = r.headers.get("content-disposition", "")
    result_name = (
        unquote(disposition.split("filename*=UTF-8''")[-1]) if "UTF-8''" in disposition else ""
    )
    check("结果文件名换成 .docx", result_name == "批改_e2e.docx", disposition)
    if r.status_code == 200:
        parsed = Document(io.BytesIO(r.content))
        text = "\n".join(p.text for p in parsed.paragraphs)
        check("PDF 的 diff 含修订文", "高兴地跳了起来" in text, text[:200])

    print("\n===== 9. 计量旁路写入 llm_calls =====")
    sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
    import database

    with database.SessionLocal() as db:
        from models.llm_call import LlmCall

        call = db.query(LlmCall).filter(LlmCall.log_id == log_id).first()
        check("有计量行", call is not None)
        if call:
            check("purpose=grading", call.purpose == "grading", call.purpose)
            check("status=success", call.status == "success", call.status)
            check("token 来自响应", call.total_tokens == 1468, str(call.total_tokens))
            check("model_name 取响应", call.model_name == "answered-model", call.model_name)
            check("latency_ms > 0", call.latency_ms > 0, str(call.latency_ms))

    print("\n===== 10. 删 provider → SET NULL，name 快照保留 =====")
    r = c.delete(f"{API}/providers/{provider_id}", headers=tok)
    check("DELETE provider 204", r.status_code == 204, str(r.status_code))
    with database.SessionLocal() as db:
        from models.llm_call import LlmCall

        call = db.query(LlmCall).filter(LlmCall.log_id == log_id).first()
        check("计量行仍在", call is not None)
        if call:
            check("provider_id 置空", call.provider_id is None)
            check("provider_name 快照仍在", call.provider_name == "mock-text", call.provider_name)

    c.delete(f"{API}/tasks/{task_id}", headers=tok)
    c.close()
    print("\n===== 汇总 =====")
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("账号 e2e_s_*、日志、落盘文件未自动清理，见 AGENTS.md")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
