"""端到端集成验证：规范库入库 → 向量检索 → 条款注入批改（阶段 6）

需要三个东西（两个进程 + 一个本地模型服务）：
  1) cd server && uv run uvicorn test_llm_server:app --port 19000
  2) cd server && LLM_TRUST_ENV_PROXY=false uv run python run_backend.py
  3) 本地 embedding 端点在跑（LM Studio 的 bge-m3，端口见 server/.env 的 EMBEDDING_BASE_URL）
  4) cd server && uv run python test_e2e_rag.py

和前两套 e2e 的分工：`test_e2e_structural.py` 证的是**没有规范库时**批改链路完好，
这一套证的是**有规范库时**条款确实进了 prompt、引用确实落进了结果。
全程零真实 token（批改走假 LLM），只有本地编码，不花钱。

跑完自动清理：本次注册的账号、日志、计量行、任务、提供商、规范文档全部删除。
落盘文件（uploads/）因 UUID 存储名无法精确定位，需偶尔手工清。

默认端口 8000 / 19000。被占用时用 FA_API 与 FA_MOCK_LLM 同时覆盖。
"""

import io
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from docx import Document

API = os.environ.get("FA_API", "http://127.0.0.1:8000/api/v1")
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


def wait_backend() -> bool:
    health_url = API.rsplit("/api/", 1)[0] + "/api/health"
    for _ in range(15):
        try:
            if httpx.get(health_url, timeout=3, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def _spec_docx() -> bytes:
    """三条四段式条款的规范文档。条款号刻意分散在不同章，检索必须能分别命中"""
    buf = io.BytesIO()
    doc = Document()
    for line in [
        "第1章 错别字类",
        "1.1.1 同音别字",
        "【适用情形】文本中出现读音相同但含义不同的汉字。",
        "【判定标准】替换为同音字后语义不通或词义改变，即判定为错别字。",
        "【错误示例】再接再励。",
        "【正确示例】再接再厉。",
        "第2章 语法类",
        "2.1.1 的字误用",
        "【适用情形】状语后面误用了的。",
        "【判定标准】修饰动词作状语时应用地，不用的是。",
        "【错误示例】高兴的说。",
        "【正确示例】高兴地说。",
        "4.1.2 顿号误用",
        "【适用情形】并列谓语之间误用顿号。",
        "【判定标准】并列谓语或分句之间应用逗号，不用顿号。",
        "【错误示例】他跑、跳都很厉害。",
        "【正确示例】他跑，跳都很厉害。",
    ]:
        doc.add_paragraph(line)
    doc.save(buf)
    return buf.getvalue()


def _essay_docx() -> bytes:
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("这是一篇测试作文。")
    doc.add_paragraph("他高兴的跳了起来，接着又再接再励地练习。")
    doc.save(buf)
    return buf.getvalue()


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _db_state(user_id: int, doc_id: int | None):
    """直接查库：向量是否真的写进去了、信封里 refs 是什么"""
    import database
    from models.operation_log import OperationLog
    from models.spec_doc import SpecChunk

    with database.SessionLocal() as db:
        chunks = db.query(SpecChunk).filter(SpecChunk.doc_id == doc_id).all() if doc_id else []
        vector_info = [
            (c.embedding is not None, len(c.embedding) if c.embedding is not None else 0)
            for c in chunks
        ]
        logs = (
            db.query(OperationLog)
            .filter(OperationLog.user_id == user_id)
            .order_by(OperationLog.id.desc())
            .all()
        )
        structured = logs[0].structured_output if logs else None
    return vector_info, structured


def _poll(client, url, tok, tries: int = 40):
    """轮到终态。上限 40 × 1.5s，与前端 POLL_INTERVAL/MAX_POLL_COUNT 同量级"""
    data: dict = {"status": "no_poll"}
    for _ in range(tries):
        time.sleep(1.5)
        r = client.get(url, headers=tok)
        # 非 200 也要能看出来：否则断言只报 KeyError，看不出是被谁拒的
        data = (
            r.json()
            if r.status_code == 200
            else {"status": f"http_{r.status_code}", "body": r.text[:200]}
        )
        if data.get("status") in ("done", "failed") or str(data.get("status")).startswith("http_"):
            return data
    return data


def _cleanup(
    user_id,
    other_user_id,
    provider_ids,
    task_ids,
    log_ids,
    spec_ids,
    username,
    other_name,
):
    import database
    from models.llm_call import LlmCall
    from models.operation_log import OperationLog
    from models.provider import ModelProvider
    from models.spec_doc import SpecChunk, SpecDoc
    from models.task import Task
    from models.user import User

    with database.SessionLocal() as db:
        for uid in (user_id, other_user_id):
            if uid:
                db.query(LlmCall).filter(LlmCall.user_id == uid).delete()
                # 注册默认初始化的任务/参考文档按用户清干净
                db.query(Task).filter(Task.user_id == uid).delete()
                for d in db.query(SpecDoc).filter(SpecDoc.user_id == uid).all():
                    db.query(SpecChunk).filter(SpecChunk.doc_id == d.id).delete()
                    db.delete(d)
        for lid in log_ids:
            db.query(OperationLog).filter(OperationLog.id == lid).delete()
        for tid in task_ids:
            db.query(Task).filter(Task.id == tid).delete()
        for pid in provider_ids:
            db.query(ModelProvider).filter(ModelProvider.id == pid).delete()
        for did in spec_ids:
            db.query(SpecChunk).filter(SpecChunk.doc_id == did).delete()
            db.query(SpecDoc).filter(SpecDoc.id == did).delete()
        for name in (username, other_name):
            db.query(User).filter(User.username == name).delete()
        db.commit()


def main() -> int:
    if not wait_backend():
        print(f"后端不在 {API.rsplit('/api/', 1)[0]}")
        return 2
    try:
        httpx.get(f"{LLM}/models", timeout=2, trust_env=False)
    except httpx.HTTPError:
        print(f"假 LLM 端点不在 {LLM}，请先 uv run uvicorn test_llm_server:app --port 19000")
        return 3

    # 编码端点探活：这一套 e2e 依赖它，缺了必须响亮退出而不是让断言一片红
    from config import get_settings

    emb_url = (get_settings().embedding_base_url or "").rstrip("/")
    try:
        httpx.get(f"{emb_url}/models", timeout=3, trust_env=False)
    except httpx.HTTPError:
        print(f"embedding 端点不在 {emb_url}，请先把本地 bge-m3 起来（LM Studio）")
        return 4

    suffix = str(int(time.time()))[-6:]
    user = f"e2e_r_{suffix}"
    other = f"e2e_r2_{suffix}"
    c = httpx.Client(trust_env=False, timeout=60.0)
    tok: dict[str, str] = {}
    other_tok: dict[str, str] = {}
    user_id = other_id = None
    provider_ids: list[int] = []
    task_ids: list[int] = []
    log_ids: list[int] = []
    spec_ids: list[int] = []

    try:
        print("\n===== 1. 两个账号：一个有规范库，一个没有 =====")
        r = c.post(f"{API}/auth/register", json={"username": user, "password": "secret123"})
        check("主账号注册 201", r.status_code == 201, r.text)
        user_id = r.json().get("id") if r.status_code == 201 else None
        r = c.post(f"{API}/auth/login", json={"username": user, "password": "secret123"})
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = c.post(f"{API}/auth/register", json={"username": other, "password": "secret123"})
        other_id = r.json().get("id") if r.status_code == 201 else None
        r = c.post(f"{API}/auth/login", json={"username": other, "password": "secret123"})
        other_tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
        # 他人账号也带默认参考文档——第 10 节要测"无规范库"，先清零
        for d in c.get(f"{API}/specs", headers=other_tok).json():
            rr = c.delete(f"{API}/specs/{d['id']}", headers=other_tok)
            check(f"他人预置规范清零（id={d['id']}）", rr.status_code == 204, rr.text[:200])

        print("\n===== 2. 提供商与批改任务 =====")
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
        provider_id = r.json()["id"]
        provider_ids.append(provider_id)
        r = c.post(
            f"{API}/tasks",
            headers=tok,
            json={
                "name": "带规范批改",
                "description": "",
                "prompt_template": "请批改作文：\n\n{{content}}",
                "is_grading": True,
                "provider_id": provider_id,
            },
        )
        check("POST task 201", r.status_code == 201, r.text)
        task_id = r.json()["id"]
        task_ids.append(task_id)

        print("\n===== 3. 规范文档入库（同步编码） =====")
        # 注册默认初始化会给账号带一份同名教学示例稿规范——先清空，
        # 保证"删库后归零"与召回断言面对的是确定性起点（只测本用例上传的那份）
        for d in c.get(f"{API}/specs", headers=tok).json():
            rr = c.delete(f"{API}/specs/{d['id']}", headers=tok)
            check(f"预置规范清零（id={d['id']}）", rr.status_code == 204, rr.text[:200])
        raw = _spec_docx()
        r = c.post(
            f"{API}/specs/upload",
            headers=tok,
            files={"file": ("spec.docx", raw, DOCX_MIME)},
            data={"title": "文字规范（e2e）"},
        )
        check("POST /specs/upload 201", r.status_code == 201, r.text[:300])
        spec_id = r.json().get("id")
        if spec_id:
            spec_ids.append(spec_id)
        check("切出 3 条条款", r.json().get("chunk_count") == 3, str(r.json()))

        print("\n===== 4. 向量真的写进库了（不是只有文本） =====")
        vectors, _ = _db_state(user_id, spec_id)
        check("每块都有向量", bool(vectors) and all(v[0] for v in vectors), str(vectors))
        check("维度都是 1024", all(v[1] == 1024 for v in vectors), str(vectors))

        print("\n===== 5. 列表与隔离 =====")
        r = c.get(f"{API}/specs", headers=tok)
        check("本人能看到自己那份", any(d["id"] == spec_id for d in r.json()), r.text[:200])
        r = c.get(f"{API}/specs", headers=other_tok)
        check(
            "他人列表不含本人规范（数据隔离）",
            r.status_code == 200 and all(d["id"] != spec_id for d in r.json()),
            r.text[:200],
        )
        r = c.get(f"{API}/specs/{spec_id}/chunks", headers=other_tok)
        check("越权读别人的规范块 404", r.status_code == 404, r.text[:200])

        print("\n===== 6. 处理含错的作文：条款应被召回并注入 =====")
        c.post(f"{LLM}/_mock/reset")
        r = c.post(
            f"{API}/upload?task_id={task_id}",
            headers=tok,
            files={"file": ("e2e.docx", _essay_docx(), DOCX_MIME)},
        )
        check("上传 201", r.status_code == 201, r.text[:200])
        log_id = r.json()["log_id"]
        log_ids.append(log_id)

        r = c.post(f"{API}/process/{log_id}", headers=tok)
        check("POST /process 返回 processing", r.json().get("status") == "processing", r.text[:200])

        status = None
        for _ in range(40):
            time.sleep(1.5)
            status = c.get(f"{API}/process/{log_id}/status", headers=tok).json()
            if status["status"] in ("done", "failed"):
                break
        check("处理完成 done", status and status["status"] == "done", str(status)[:300])

        refs = (status or {}).get("spec_refs") or []
        check("status 带回 spec_refs 非空", len(refs) >= 1, str(refs))
        # 混合召回必须把作文里真正踩到的那条错别字条款捞回来：
        # 作文里的"再接再励"就是规范 1.1.1 举过的错例，捞不到就是检索没干活
        check(
            "召回了作文实际踩到的条款 1.1.1",
            any(x["clause"] == "1.1.1" for x in refs),
            str(refs),
        )
        check(
            "每条引用都标了召回来源 via",
            all(x.get("via") in ("vector", "lexical", "both") for x in refs),
            str(refs),
        )
        check(
            "每条引用都有条款号与分数",
            all(
                re.match(r"^\d+(\.\d+)+", x["clause"]) and isinstance(x["score"], float)
                for x in refs
            ),
            str(refs),
        )
        check(
            "spec_note 为空（检索没降级）", not (status or {}).get("spec_note"), str(status)[:120]
        )

        print("\n===== 7. 条款确实进了 prompt，而且没污染契约 =====")
        last = c.get(f"{LLM}/_mock/last").json().get("messages", [])
        check("假 LLM 收到了一次批改请求", len(last) == 2, str(last)[:200])
        system_msg = next((m["content"] for m in last if m["role"] == "system"), "")
        user_msg = next((m["content"] for m in last if m["role"] == "user"), "")
        check("user 里带了召回的条款原文", "判定标准" in user_msg, user_msg[:300])
        check("user 里带了条款号", any(x["clause"] in user_msg for x in refs), user_msg[:300])
        check("user 里写明不适用不要硬套", "不要引用" in user_msg, user_msg[:300])
        check("system 契约未被条款污染", "判定标准" not in system_msg, system_msg[:200])
        check("system 仍是原契约", "revised_text" in system_msg, system_msg[:200])

        print("\n===== 8. 引用落进了 structured_output（可溯源） =====")
        _, structured = _db_state(user_id, spec_id)
        check(
            "structured_output 有 refs 字段", '"refs"' in (structured or ""), str(structured)[:120]
        )

        print("\n===== 9. 下载链路不受影响 =====")
        r = c.get(f"{API}/download/{log_id}", headers=tok)
        check(
            "下载 diff 200 且是 docx",
            r.status_code == 200 and r.content[:2] == b"PK",
            str(r.status_code),
        )
        # 依据条款那一页必须真的进了交付件：界面上有、下载没有，等于溯源只在屏幕上活着
        delivered = Document(io.BytesIO(r.content))
        body = "\n".join(para.text for para in delivered.paragraphs)
        check("交付 docx 含「依据条款」页", "本次批改依据的规范条款" in body, body[-200:])
        refs_page = body.split("本次批改依据的规范条款", 1)[1]
        check(
            "交付 docx 列出了召回的条款号",
            all(x["clause"] in refs_page for x in refs),
            refs_page[:200],
        )
        # 只查引用页：正文里出现条款全文是模型自己回显的（假 LLM 会把整条 prompt 当修订文返回），
        # 不是交付件转载的——那一页才是我们控制的边界
        check(
            "引用页只列条款号与标题，不转载条文", "【判定标准】" not in refs_page, refs_page[:200]
        )

        print("\n===== 10. 没建规范库的账号：批改链路完好且不注入 =====")
        # 提供商按 user 隔离，第二个账号必须有自己的 provider + task，不能借用
        r = c.post(
            f"{API}/providers",
            headers=other_tok,
            json={
                "name": "mock-other",
                "base_url": LLM,
                "api_key": "sk-mock",
                "model_name": "mock",
            },
        )
        check("他人建自己的提供商 201", r.status_code == 201, r.text[:200])
        provider_ids.append(r.json()["id"])

        r = c.post(
            f"{API}/tasks",
            headers=other_tok,
            json={
                "name": "无规范批改",
                "description": "",
                "prompt_template": "请批改作文：\n\n{{content}}",
                "is_grading": True,
                "provider_id": provider_ids[-1],
            },
        )
        check("他人建自己的任务 201", r.status_code == 201, r.text[:200])
        other_task_id = r.json()["id"]
        task_ids.append(other_task_id)

        r = c.post(
            f"{API}/upload?task_id={task_id}",
            headers=other_tok,
            files={"file": ("e2e2.docx", _essay_docx(), DOCX_MIME)},
        )
        check("他人引用主账号的任务被拒", r.status_code in (400, 404), r.text[:200])

        c.post(f"{LLM}/_mock/reset")
        r = c.post(
            f"{API}/upload?task_id={other_task_id}",
            headers=other_tok,
            files={"file": ("e2e2.docx", _essay_docx(), DOCX_MIME)},
        )
        check("他人上传到自己的任务 201", r.status_code == 201, r.text[:200])
        other_log_id = r.json()["log_id"]
        log_ids.append(other_log_id)
        c.post(f"{API}/process/{other_log_id}", headers=other_tok)
        status_other = _poll(c, f"{API}/process/{other_log_id}/status", other_tok)
        check("无规范库也照样批完 done", status_other["status"] == "done", str(status_other)[:200])
        check(
            "问题清单照常产出（批改本身没被 RAG 路径改动）",
            bool(status_other.get("issues")),
            str(status_other.get("issues"))[:120],
        )
        check("spec_refs 为空", not status_other.get("spec_refs"), str(status_other))
        check("spec_note 也没有（是没库，不是查不了）", not status_other.get("spec_note"))
        last_other = c.get(f"{LLM}/_mock/last").json().get("messages", [])
        check("假 LLM 确实收到了这一次请求", len(last_other) == 2, str(last_other)[:120])
        user_other = next((m["content"] for m in last_other if m["role"] == "user"), "")
        check("prompt 里没有条款上下文", "判定标准" not in user_other, user_other[:200])

        print("\n===== 11. 主账号删掉规范后重新处理：引用归零 =====")
        r = c.delete(f"{API}/specs/{spec_id}", headers=tok)
        check("DELETE /specs 204", r.status_code == 204, r.text[:200])
        if spec_id in spec_ids:
            spec_ids.remove(spec_id)

        # 必须换一份新日志：已 done 的日志后端拒绝重处理（仅 pending/failed 可重试）。
        # 拿旧日志断言只会读到上一轮留下的 refs，那是在测缓存而不是测这条路径。
        c.post(f"{LLM}/_mock/reset")
        r = c.post(
            f"{API}/upload?task_id={task_id}",
            headers=tok,
            files={"file": ("e2e3.docx", _essay_docx(), DOCX_MIME)},
        )
        check("删库后重新上传 201", r.status_code == 201, r.text[:200])
        log_id2 = r.json()["log_id"]
        log_ids.append(log_id2)
        r = c.post(f"{API}/process/{log_id2}", headers=tok)
        check("重处理返回 processing", r.json().get("status") == "processing", r.text[:200])
        status2 = _poll(c, f"{API}/process/{log_id2}/status", tok)
        check("删除规范后仍处理成功", status2["status"] == "done", str(status2)[:200])
        check("spec_refs 变空", not status2.get("spec_refs"), str(status2.get("spec_refs")))
        check("spec_note 也没有（是没库而非失败）", not status2.get("spec_note"))
        last2 = c.get(f"{LLM}/_mock/last").json().get("messages", [])
        check("假 LLM 收到了删库后的这一次", len(last2) == 2, str(last2)[:120])
        user2 = next((m["content"] for m in last2 if m["role"] == "user"), "")
        check("prompt 里不再出现条款上下文", "判定标准" not in user2, user2[:200])
        return 0
    finally:
        print("\n===== 清理 =====")
        _cleanup(user_id, other_id, provider_ids, task_ids, log_ids, spec_ids, user, other)
        print(f"  已清理 DB（user={user_id}, other={other_id}, specs={spec_ids}）")
        print(f"\n结果：{PASS} 通过 / {FAIL} 失败")


if __name__ == "__main__":
    sys.exit(main())
