"""端到端集成验证：PDF → MinerU 版面块 → （片2 VLM 坏块修复）→ .pptx 下载，零真实费用。

需要两个假端点进程（两个终端）：
  1) cd server && uv run uvicorn test_mineru_server:app --port 19002
  2) cd server && uv run uvicorn test_vlm_server:app --port 19001
然后：
  3) cd server && uv run python test_e2e_ppt.py

后端由本脚本自己拉起（FA_PPT_E2E_PORT，默认 8011），注入 MINERU_TOKEN /
MINERU_BASE_URL 指向假 MinerU——日常 .env 不该为跑测试改动。
⚠ 仍走 run_backend.py 入口（Windows Selector policy，见 AGENTS.md §2）。
片1 转换路径零文本 LLM；片2 只在有坏块时打假 VLM。

验证点：
- pptx + 批改互斥（创建 422 / 更新 400）；pptx 任务只收 PDF（docx 上传 400）
- 文字型 PDF 也走 MinerU（要版面块不是纯文本），处理 done
- 交付 .pptx 可被 python-pptx 重读：正文进可编辑文本框、表格进原生表、图在、
  横向页页眉保留（A 整页忠实）、页码噪声跳过
- 无坏块时零 llm_calls（MinerU 按页计费无 token，渲染纯本地）
- structured 三件套 + outputs/pptx/{log_id}.pptx 落盘
- MinerU failed → 日志 failed 文案可行动；重置后重试同一日志 → done
- 越权下载 404
- 片2：坏块样本 → VLM 带区域原图修复 → 产物含修复文字/表格 + "LLM 修复"标记，
  llm_calls 有 ppt_repair 行且归 vision provider；正常块不受影响
- 片3：版面批评循环（stub 渲染）→ 第一轮点名幻灯片 1 → 该横向页改走流式重排
  （页眉不再出现、正文保留）→ 第二轮 pass；llm_calls 有 2 行 ppt_critic

跑完自动清理：DB 记录、outputs/mineru/{log_id}/、outputs/pptx/{log_id}.pptx、
uploads 里本次的落盘文件（按 stored_filename 精确回溯）。
"""

import io
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import pymupdf
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FA_PPT_E2E_PORT", "8011"))
API = f"http://127.0.0.1:{PORT}/api/v1"
MINERU = os.environ.get("FA_MOCK_MINERU", "http://127.0.0.1:19002")
VLM = os.environ.get("FA_MOCK_VLM", "http://127.0.0.1:19001/v1")
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


def text_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "这是一篇文字型 PDF。", fontname="china-s", fontsize=11)
    page.insert_text((72, 92), "他高兴的跳了起来。", fontname="china-s", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def start_backend() -> subprocess.Popen:
    env = {
        **os.environ,
        "FA_PORT": str(PORT),
        "MINERU_TOKEN": MOCK_TOKEN,
        "MINERU_BASE_URL": MINERU,
        "MINERU_POLL_INTERVAL": "0.2",
        "MINERU_MAX_WAIT": "30",
        "LLM_TRUST_ENV_PROXY": "false",
        # 假 MinerU / 假 VLM 都在 127.0.0.1，SSRF 闸默认拒绝私有地址；日常 .env 里这行
        # 是注释掉的，由脚本自己注入，这样"别人也能用"的安全默认不会挡住端到端回归
        "ALLOW_PRIVATE_PROVIDER_URLS": "true",
        # 版面循环走 stub 渲染（1×1 占位 PNG）：e2e 测的是循环机制与计量归因，
        # 真 soffice 渲染留给本机日常与人工目检，不在 CI 路径里烧 WSL 时间
        "PPT_RENDER_BACKEND": "stub",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    log = open(os.path.join(SERVER_DIR, "_e2e_ppt_backend.log"), "w", encoding="utf-8")
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


def llm_calls_for(user_id: int) -> list[tuple]:
    import database
    from models.llm_call import LlmCall

    with database.SessionLocal() as db:
        rows = db.query(LlmCall).filter(LlmCall.user_id == user_id).all()
        return [(r.purpose, r.provider_name, r.status) for r in rows]


def cleanup(user_id, task_ids, log_ids, provider_ids=()):
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
        for pid in provider_ids:
            db.query(ModelProvider).filter(ModelProvider.id == pid).delete()
        if user_id:
            # 注册默认初始化的任务/参考文档按用户清干净，否则 user 行删不掉
            from models.spec_doc import SpecChunk, SpecDoc

            db.query(Task).filter(Task.user_id == user_id).delete()
            for d in db.query(SpecDoc).filter(SpecDoc.user_id == user_id).all():
                db.query(SpecChunk).filter(SpecChunk.doc_id == d.id).delete()
                db.delete(d)
            db.query(User).filter(User.id == user_id).delete()
        db.commit()

    for lid in log_ids:
        shutil.rmtree(os.path.join(SERVER_DIR, "outputs", "mineru", str(lid)), ignore_errors=True)
        try:
            os.remove(os.path.join(SERVER_DIR, "outputs", "pptx", f"{lid}.pptx"))
        except OSError:
            pass
    for name in stored_names:
        try:
            os.remove(os.path.join(SERVER_DIR, "uploads", name))
        except OSError:
            pass


def main():
    try:
        httpx.post(f"{MINERU}/_mock/reset", timeout=2, trust_env=False)
    except httpx.HTTPError:
        print(
            f"假 MinerU 端点不在 {MINERU}，请先 uv run uvicorn test_mineru_server:app --port 19002"
        )
        sys.exit(3)
    try:
        httpx.post(f"{VLM}/_mock/reset", timeout=2, trust_env=False)
    except httpx.HTTPError:
        print(f"假 VLM 端点不在 {VLM}，请先 uv run uvicorn test_vlm_server:app --port 19001")
        sys.exit(3)

    proc = start_backend()
    if not wait_backend():
        print(f"后端没起来（{PORT}），看 server/_e2e_ppt_backend.log")
        proc.terminate()
        sys.exit(2)

    suffix = str(int(time.time()))[-6:]
    c = httpx.Client(trust_env=False, timeout=30.0)
    user = f"e2e_p_{suffix}"
    tok: dict[str, str] = {}
    user_id = task_id = other_id = vision_provider_id = None
    log_ids: list[int] = []

    try:
        print("\n===== 1. 注册 + 登录 =====")
        r = c.post(f"{API}/auth/register", json={"username": user, "password": "secret123"})
        check("注册 201", r.status_code == 201, r.text)
        user_id = r.json().get("id") if r.status_code == 201 else None
        r = c.post(f"{API}/auth/login", json={"username": user, "password": "secret123"})
        check("登录 200", r.status_code == 200, r.text)
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n===== 2. 任务约束：互斥 + 只收 PDF =====")
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "非法组合",
                "prompt_template": "{{content}}",
                "is_grading": True,
                "output_format": "pptx",
            },
        )
        check("pptx+批改创建 422", r.status_code == 422, r.text)
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "PDF转PPT",
                "description": "",
                "prompt_template": "{{content}}",
                "is_grading": False,
                "output_format": "pptx",
            },
        )
        check("pptx 任务创建 201", r.status_code == 201, r.text)
        task_id = r.json()["id"]
        r = c.put(f"{API}/tasks/{task_id}", headers=tok, json={"is_grading": True})
        check("更新成批改 400", r.status_code == 400, r.text)
        r = upload(c, tok, task_id, "样本.docx", b"not-a-real-docx")
        check("docx 上传 400", r.status_code == 400 and ".pdf" in r.text, r.text)

        print("\n===== 3. 文字型 PDF 上传（也走 MinerU 版面块） =====")
        r = upload(c, tok, task_id, "文字样本.pdf", text_pdf())
        check("上传 201", r.status_code == 201, r.text)
        body = r.json()
        log_id = body["log_id"]
        log_ids.append(log_id)
        check("文字型不带 ocr_pending", body.get("ocr_pending") is False, r.text)
        check("上传即有提取预览", body.get("content_length", 0) > 0, r.text)

        print("\n===== 4. 处理 → done，无坏块零外呼 =====")
        r = c.post(f"{API}/process/{log_id}", headers=tok)
        check("POST /process 200", r.status_code == 200, r.text)
        check("立即返回 processing", r.json().get("status") == "processing", r.text)
        final = poll_final(c, tok, log_id, "done")
        check("状态 done", final.get("status") == "done", str(final)[:300])
        check(
            "processed_content 是 markdown", "扫描件标题" in (final.get("processed_content") or "")
        )
        check("非批改：issues 透 None", final.get("issues") is None, str(final)[:300])
        check("非批改：不报 degraded", final.get("grading_degraded") is False)
        check(
            "original_content 保留上传预览", "文字型 PDF" in (final.get("original_content") or "")
        )
        check("无坏块时零 llm_calls", not llm_calls_for(user_id))

        print("\n===== 5. 下载 .pptx：可编辑 + 完整 =====")
        r = c.get(f"{API}/download/{log_id}", headers=tok)
        check("下载 200", r.status_code == 200, r.text[:200])
        check(
            "媒体类型 pptx",
            r.headers.get("content-type", "").startswith(
                "application/vnd.openxmlformats-officedocument.presentationml"
            ),
            r.headers.get("content-type", ""),
        )
        disp = r.headers.get("content-disposition", "")
        check("RFC5987 中文文件名", f"filename*=UTF-8''{quote('转换_文字样本.pptx')}" in disp, disp)
        prs = Presentation(io.BytesIO(r.content))
        slides = list(prs.slides)
        check("幻灯片 ≥2 页（流式页 + 横向兜底页）", len(slides) >= 2, str(len(slides)))
        texts = [s.text_frame.text for slide in slides for s in slide.shapes if s.has_text_frame]
        joined = "\n".join(texts)
        check("标题进可编辑文本框", "扫描件标题" in texts, joined[:200])
        check("正文逐字搬运", "他高兴的跳了起来。" in texts and "结尾段。" in texts, joined[:200])
        check("横向页页眉保留（A 兜底）", "横向页标题" in texts, joined[:200])
        check("横向页正文在", "横向页正文。" in texts, joined[:200])
        check("页码噪声跳过", "2" not in texts, joined[:200])
        tables = [s.table for slide in slides for s in slide.shapes if s.has_table]
        check("表格进原生可编辑表", len(tables) >= 1, str(len(tables)))
        if tables:
            check("表格单元格内容在", tables[0].cell(0, 0).text == "列A")
        pics = [
            s for slide in slides for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE
        ]
        check("图片贴进幻灯片", len(pics) >= 1, str(len(pics)))

        print("\n===== 6. 落盘产物 =====")
        base = os.path.join(SERVER_DIR, "outputs", "mineru", str(log_id))
        check("full.md 落盘", os.path.isfile(os.path.join(base, "full.md")))
        check("content_list.json 落盘", os.path.isfile(os.path.join(base, "content_list.json")))
        check("images/a.jpg 落盘", os.path.isfile(os.path.join(base, "images", "a.jpg")))
        check(
            "pptx 交付件落盘",
            os.path.isfile(os.path.join(SERVER_DIR, "outputs", "pptx", f"{log_id}.pptx")),
        )

        print("\n===== 7. MinerU 失败 → failed 可行动 → 重试 done =====")
        httpx.post(f"{MINERU}/_mock/mode", json={"mode": "failed"}, timeout=5, trust_env=False)
        r = upload(c, tok, task_id, "失败样本.pdf", text_pdf())
        check("失败样本上传 201", r.status_code == 201, r.text)
        fail_log = r.json()["log_id"]
        log_ids.append(fail_log)
        c.post(f"{API}/process/{fail_log}", headers=tok)
        final = poll_final(c, tok, fail_log, "failed")
        check("MinerU failed → 日志 failed", final.get("status") == "failed", str(final)[:300])
        check(
            "错误文案可行动",
            "PDF 转 PPT 失败" in (final.get("error_message") or ""),
            str(final)[:300],
        )
        check("失败路径零 llm_calls", not llm_calls_for(user_id))
        httpx.post(f"{MINERU}/_mock/reset", timeout=5, trust_env=False)
        r = c.post(f"{API}/process/{fail_log}", headers=tok)
        check("failed 可重试", r.status_code == 200, r.text)
        final = poll_final(c, tok, fail_log, "done")
        check("重试后 done", final.get("status") == "done", str(final)[:300])

        print("\n===== 8. 越权下载 404 =====")
        other = f"e2e_p_other_{suffix}"
        r = c.post(f"{API}/auth/register", json={"username": other, "password": "secret123"})
        other_id = r.json().get("id") if r.status_code == 201 else None
        r = c.post(f"{API}/auth/login", json={"username": other, "password": "secret123"})
        other_tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = c.get(f"{API}/download/{log_id}", headers=other_tok)
        check("他人下载 404", r.status_code == 404, r.text[:200])

        print("\n===== 9. 片2：坏块 → VLM 区域原图修复 → 标记 + 计量 =====")
        r = c.post(
            f"{API}/providers",
            headers=tok,
            json={
                "name": "mock-vision",
                "base_url": VLM,
                "api_key": "sk-mock",
                "model_name": "mock-vlm",
                "capabilities": "vision",
            },
        )
        check("vision provider 201", r.status_code == 201, r.text)
        vision_provider_id = r.json()["id"]
        httpx.post(f"{MINERU}/_mock/mode", json={"mode": "defective"}, timeout=5, trust_env=False)
        httpx.post(f"{VLM}/_mock/reset", timeout=5, trust_env=False)
        r = upload(c, tok, task_id, "缺陷样本.pdf", text_pdf())
        check("缺陷样本上传 201", r.status_code == 201, r.text)
        rep_log = r.json()["log_id"]
        log_ids.append(rep_log)
        c.post(f"{API}/process/{rep_log}", headers=tok)
        final = poll_final(c, tok, rep_log, "done")
        check("缺陷样本 done", final.get("status") == "done", str(final)[:300])

        stats = httpx.get(f"{VLM}/_mock/repairs", timeout=5, trust_env=False).json()
        repairs = stats.get("repairs", {}).get(f"log-{rep_log}", 0)
        check("VLM 修复外呼 2 次（空段落 + 坏表格）", repairs == 2, str(stats))

        r = c.get(f"{API}/download/{rep_log}", headers=tok)
        prs = Presentation(io.BytesIO(r.content))
        texts = [
            s.text_frame.text for slide in prs.slides for s in slide.shapes if s.has_text_frame
        ]
        joined = "\n".join(texts)
        check("修复文字进产物", "VLM 修复的正文。" in texts, joined[:300])
        check("修复块带 LLM 修复标记", any("LLM 修复" in t for t in texts), joined[:300])
        check("正常块不受影响", "正常段落不受影响。" in texts, joined[:300])
        rep_tables = [s.table for slide in prs.slides for s in slide.shapes if s.has_table]
        check("坏表格修成原生表", len(rep_tables) >= 1, str(len(rep_tables)))
        if rep_tables:
            check("修复表格单元格在", rep_tables[0].cell(0, 0).text == "修复列A")

        calls = llm_calls_for(user_id)
        repair_rows = [row for row in calls if row[0] in ("ppt_repair", "ppt_repair_degraded")]
        check("计量有 2 行 ppt_repair", len(repair_rows) == 2, str(calls))
        check(
            "修复行归 vision provider",
            all(r[1] == "mock-vision" for r in repair_rows),
            str(repair_rows),
        )
        check("修复行全部 success", all(r[2] == "success" for r in repair_rows), str(repair_rows))
        httpx.post(f"{MINERU}/_mock/reset", timeout=5, trust_env=False)

        print("\n===== 10. 片3：版面批评循环 → 横向页改走重排 → pass =====")
        httpx.post(f"{VLM}/_mock/reset", timeout=5, trust_env=False)
        critic_rows_before = len(
            [r for r in llm_calls_for(user_id) if r[0].startswith("ppt_critic")]
        )
        r = upload(c, tok, task_id, "版面样本.pdf", text_pdf())
        check("版面样本上传 201", r.status_code == 201, r.text)
        lay_log = r.json()["log_id"]
        log_ids.append(lay_log)
        c.post(f"{API}/process/{lay_log}", headers=tok)
        final = poll_final(c, tok, lay_log, "done")
        check("版面样本 done", final.get("status") == "done", str(final)[:300])
        lay_stats = httpx.get(f"{VLM}/_mock/repairs", timeout=5, trust_env=False).json()
        lay_calls = sum((lay_stats.get("layout") or {}).values())
        check("版面批评外呼 2 轮（挑刺→通过）", lay_calls == 2, str(lay_stats))
        critic_rows = [r for r in llm_calls_for(user_id) if r[0].startswith("ppt_critic")]
        check(
            "新增 2 行 ppt_critic 计量",
            len(critic_rows) - critic_rows_before == 2,
            str(critic_rows),
        )
        check("批评行归 vision provider", all(r[1] == "mock-vision" for r in critic_rows))
        r = c.get(f"{API}/download/{lay_log}", headers=tok)
        prs = Presentation(io.BytesIO(r.content))
        texts = [
            s.text_frame.text for slide in prs.slides for s in slide.shapes if s.has_text_frame
        ]
        joined = "\n".join(texts)
        check("被点名的横向页改走重排：页眉不再原位出现", "横向页标题" not in texts, joined[:300])
        check("重排后正文仍在", "横向页正文。" in texts, joined[:300])
        check("文字内容一字未改（其余照旧）", "结尾段。" in texts and "扫描件标题" in texts)
    finally:
        c.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        cleanup(
            user_id,
            [task_id] if task_id else [],
            log_ids,
            [vision_provider_id] if vision_provider_id else [],
        )
        if other_id:
            import database
            from models.spec_doc import SpecChunk, SpecDoc
            from models.task import Task
            from models.user import User

            with database.SessionLocal() as db:
                db.query(Task).filter(Task.user_id == other_id).delete()
                for d in db.query(SpecDoc).filter(SpecDoc.user_id == other_id).all():
                    db.query(SpecChunk).filter(SpecChunk.doc_id == d.id).delete()
                    db.delete(d)
                db.query(User).filter(User.id == other_id).delete()
                db.commit()

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
