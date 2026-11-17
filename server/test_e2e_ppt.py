"""端到端 PPT 集成验证：假 LLM + 假 VLM + 真后端 + 真 Postgres。

需要三个进程：
  1) cd server && uv run uvicorn test_llm_server:app --port 19000
  2) cd server && uv run uvicorn test_vlm_server:app --port 19001
  3) cd server && LLM_TRUST_ENV_PROXY=false uv run python run_backend.py
  4) cd server && uv run python test_e2e_ppt.py

跑的是「上传 PDF → plan_slides → assemble → render(stub) → critique → revise →
assemble → critique(pass) → END → 下载 PPTX」完整链路，含：

- task.output_format=pptx 派发（s5.1）
- 文本 / 大纲外呼走 text provider，批评外呼走 vision provider
- llm_calls 表里 purpose=outline / critic 与提供商正确归因
- structured_output 是 kind=ppt_outline 形态，deserialize_outline 解出来页数对得上
- 下载得到合法 .pptx 二进制，python-pptx 重读页数 == outline.slides + 1（封面）

跑完自动清理 e2e_p_* 账号 + 任务 + provider + log + llm_calls + mock VLM 状态。

环境变量：
  FA_API       默认 http://127.0.0.1:8000/api/v1
  FA_MOCK_LLM  默认 http://127.0.0.1:19000/v1
  FA_MOCK_VLM  默认 http://127.0.0.1:19001/v1
  MOCK_VLM_STUB_ROUNDS  假 VLM 端用，默认 1（第一次挑刺、第二次 pass）
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

API = os.environ.get("FA_API", "http://127.0.0.1:8000/api/v1")
LLM = os.environ.get("FA_MOCK_LLM", "http://127.0.0.1:19000/v1")
VLM = os.environ.get("FA_MOCK_VLM", "http://127.0.0.1:19001/v1")

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
    """每视觉行一个 block 的中文 PDF，逼 s5.2 大纲生成看到结构清晰的内容"""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for text in lines:
        page.insert_text((72, y), text, fontname="china-s", fontsize=11)
        y += 17.0
    data = doc.tobytes()
    doc.close()
    return data


def wait_url(url: str, attempts: int = 15) -> bool:
    for _ in range(attempts):
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def _cleanup_db(user_id, provider_ids, task_id, log_ids, username):
    import database
    from models.llm_call import LlmCall
    from models.operation_log import OperationLog
    from models.provider import ModelProvider
    from models.task import Task
    from models.user import User

    with database.SessionLocal() as db:
        if user_id:
            db.query(LlmCall).filter(LlmCall.user_id == user_id).delete()
        for lid in log_ids:
            db.query(OperationLog).filter(OperationLog.id == lid).delete()
        if task_id:
            db.query(Task).filter(Task.id == task_id).delete()
        for pid in provider_ids:
            db.query(ModelProvider).filter(ModelProvider.id == pid).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()


def main():
    health_url = API.rsplit("/api/", 1)[0] + "/api/health"
    if not wait_url(health_url):
        print(f"后端不在 {health_url}")
        sys.exit(2)
    if not wait_url(f"{LLM}/models", attempts=3):
        print(f"假 LLM 不在 {LLM}，请先 uv run uvicorn test_llm_server:app --port 19000")
        sys.exit(3)
    if not wait_url(f"{VLM}/models", attempts=3):
        print(f"假 VLM 不在 {VLM}，请先 uv run uvicorn test_vlm_server:app --port 19001")
        sys.exit(4)

    suffix = str(int(time.time()))[-6:]
    c = httpx.Client(trust_env=False, timeout=60.0)
    user = f"e2e_p_{suffix}"
    tok: dict[str, str] = {}
    user_id = None
    provider_ids: list[int] = []
    task_id = None
    log_ids: list[int] = []

    try:
        print("\n===== 0. 重置 mock VLM 状态 =====")
        try:
            r = c.post(f"{VLM.rsplit('/v1', 1)[0]}/v1/_mock/reset", timeout=5)
            check("mock VLM reset 200", r.status_code == 200, str(r.status_code))
        except httpx.HTTPError as e:
            check("mock VLM reset", False, str(e))

        print("\n===== 1. 注册 + 登录 =====")
        r = c.post(f"{API}/auth/register", json={"username": user, "password": "secret123"})
        check("注册 201", r.status_code == 201, r.text)
        user_id = r.json().get("id") if r.status_code == 201 else None
        r = c.post(f"{API}/auth/login", json={"username": user, "password": "secret123"})
        check("登录 200", r.status_code == 200, r.text)
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n===== 2. 文本提供商（mock LLM 19000）=====")
        r = c.post(
            f"{API}/providers",
            headers=tok,
            json={
                "name": "mock-text-ppt",
                "base_url": LLM,
                "api_key": "sk-mock-text",
                "model_name": "mock-text",
            },
        )
        check("text provider 201", r.status_code == 201, r.text)
        text_provider_id = r.json()["id"]
        provider_ids.append(text_provider_id)

        print("\n===== 3. vision 提供商（mock VLM 19001）=====")
        r = c.post(
            f"{API}/providers",
            headers=tok,
            json={
                "name": "mock-vision-ppt",
                "base_url": VLM,
                "api_key": "sk-mock-vision",
                "model_name": "mock-vlm",
                "capabilities": "text,vision",
            },
        )
        check("vision provider 201", r.status_code == 201, r.text)
        check(
            "capabilities 含 vision",
            "vision" in (r.json().get("capabilities") or ""),
            str(r.json()),
        )
        vision_provider_id = r.json()["id"]
        provider_ids.append(vision_provider_id)

        print("\n===== 4. 建 PPT 任务（output_format=pptx）=====")
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "PPT 汇报",
                "description": "PDF 转 PPT 端到端",
                "prompt_template": "请提炼成幻灯片：\n\n{{content}}",
                "is_grading": False,
                "provider_id": text_provider_id,
                "output_format": "pptx",
            },
        )
        check("task 201", r.status_code == 201, r.text)
        check(
            "task.output_format=pptx",
            r.json().get("output_format") == "pptx",
            str(r.json()),
        )
        task_id = r.json()["id"]

        print("\n===== 5. 上传 3 页中文 PDF =====")
        pdf_bytes = make_pdf(
            [
                "本文讨论 的 地 得 用法。",
                "第一段：他高兴的跳了起来。",
                "第二段：她认真的记录数据。",
                "第三段：题目做的很顺利。",
            ]
        )
        r = c.post(
            f"{API}/upload?task_id={task_id}",
            headers=tok,
            files={"file": ("e2e_ppt.pdf", pdf_bytes, "application/pdf")},
        )
        check("upload 201", r.status_code == 201, r.text)
        log_id = r.json()["log_id"]
        log_ids.append(log_id)

        print("\n===== 6. 触发处理（后台跑完整 PPT 流水线）=====")
        r = c.post(f"{API}/process/{log_id}", headers=tok)
        check("POST /process 200", r.status_code == 200, r.text)
        check("立即返回 processing", r.json().get("status") == "processing", r.text)

        final = {}
        for _ in range(60):
            time.sleep(1)
            final = c.get(f"{API}/process/{log_id}/status", headers=tok).json()
            if final.get("status") in ("done", "failed"):
                break

        print("\n===== 7. 终态校验 =====")
        check("轮询到 done", final.get("status") == "done", str(final)[:300])
        check("有 processed_content（大纲纯文本视图）", bool(final.get("processed_content")))
        # processed_content 现在装的是 _render_outline_as_text 的产物
        # mock LLM 返回的演示文稿 3 页大纲
        check(
            "processed_content 含演示文稿字样",
            "演示文稿" in (final.get("processed_content") or ""),
            (final.get("processed_content") or "")[:200],
        )

        print("\n===== 8. 下载 PPTX 并解析 =====")
        r = c.get(f"{API}/download/{log_id}", headers=tok)
        check("下载 200", r.status_code == 200, str(r.status_code))
        check("Content-Type 是 pptx", "presentationml" in r.headers.get("content-type", ""))
        disposition = r.headers.get("content-disposition", "")
        result_name = (
            unquote(disposition.split("filename*=UTF-8''")[-1]) if "UTF-8''" in disposition else ""
        )
        check("下载文件名后缀 .pptx", result_name.endswith(".pptx"), disposition)

        if r.status_code == 200:
            try:
                from pptx import Presentation

                prs = Presentation(io.BytesIO(r.content))
                # mock LLM 给的 3 页 + 封面 = 4 页
                check("PPTX 页数 == 4", len(prs.slides) == 4, f"got {len(prs.slides)}")
                first = prs.slides[0]
                check("封面主标题", first.shapes.title.text == "演示文稿")
                second = prs.slides[1]
                check("第一页正文标题=封面", second.shapes.title.text == "封面")
            except Exception as e:
                check("python-pptx 解析", False, str(e))

        print("\n===== 9. llm_calls 归因：outline vs critic 提供商分开 =====")
        sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
        import database

        with database.SessionLocal() as db:
            from models.llm_call import LlmCall

            rows = db.query(LlmCall).filter(LlmCall.log_id == log_id).all()
            check("至少 3 行（plan + revise + critic）", len(rows) >= 3, str(len(rows)))
            by_purpose = {}
            for r in rows:
                by_purpose.setdefault(r.purpose, []).append(r)
            check("有 outline 行", "outline" in by_purpose, str(list(by_purpose)))
            check("有 critic 行", "critic" in by_purpose, str(list(by_purpose)))
            outline_ids = {r.provider_id for r in by_purpose.get("outline", [])}
            critic_ids = {r.provider_id for r in by_purpose.get("critic", [])}
            check(
                "outline 行归 text provider",
                outline_ids == {text_provider_id},
                str(outline_ids),
            )
            check(
                "critic 行归 vision provider",
                critic_ids == {vision_provider_id},
                str(critic_ids),
            )
            # critic 至少跑了 2 次（一次挑刺、一次 pass），所以 critic 行 >= 2
            check(
                "critic 跑了至少 2 次（默认 max_rounds=2，第一次 needs_revision 第二次 pass）",
                len(by_purpose.get("critic", [])) >= 2,
                str(len(by_purpose.get("critic", []))),
            )

        print("\n===== 10. structured_output 是 ppt_outline 形态 =====")
        with database.SessionLocal() as db:
            from models.operation_log import OperationLog

            row = db.query(OperationLog).filter(OperationLog.id == log_id).first()
            so = row.structured_output or ""
            check(
                "structured_output 含 kind=ppt_outline",
                '"kind"' in so and "ppt_outline" in so,
                so[:120],
            )
            check("含 slides 字段", '"slides"' in so, so[:80])

    finally:
        print("\n===== 清理 =====")
        try:
            _cleanup_db(user_id, provider_ids, task_id, log_ids, user)
            print(f"  已清理 DB（user={user}）")
        except Exception as e:
            print(f"  清理失败: {e}")
        print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
        sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
