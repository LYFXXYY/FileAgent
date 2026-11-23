"""
后端端到端测试脚本 —— 覆盖认证、CRUD、上传、处理、日志

用法：cd server && uv run python test_e2e.py

跑完自动清理：删除本次注册的账号、日志、计量行、任务、提供商。
落盘文件（uploads/）因 UUID 存储名无法精确定位，需偶尔手工清。
"""

from __future__ import annotations

import io
import sys
import time
import traceback

# 强制 stdout 用 UTF-8，避免 Windows 控制台 GBK 编码问题
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from docx import Document

import e2e_support
from e2e_support import require_private_providers

BASE = "http://localhost:8000"
API = f"{BASE}/api/v1"
# trust_env=False：与 test_e2e_structural.py 一致。e2e 只打 localhost，系统代理无关；
# 而本机 NO_PROXY 里有裸 `[::1]`，httpx 解析端口会炸 InvalidURL —— 不绕开就只能让脚本起不来
CLIENT = httpx.Client(timeout=30.0, trust_env=False)

# 每次运行换个后缀，避免用户名重复
SUFFIX = str(int(time.time()))[-6:]
USERNAME = f"tester_{SUFFIX}"
PASSWORD = "secret123"

PASS = 0
FAIL = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def section(title: str) -> None:
    print(f"\n===== {title} =====")


def _cleanup_db():
    """删除本次运行造的数据库记录（llm_calls → logs → tasks → providers → users）"""
    import database
    from models.llm_call import LlmCall
    from models.operation_log import OperationLog
    from models.provider import ModelProvider
    from models.spec_doc import SpecChunk, SpecDoc
    from models.task import Task
    from models.user import User

    with database.SessionLocal() as db:
        db.query(LlmCall).filter(LlmCall.user_id == user_id).delete()
        if log_id:
            db.query(OperationLog).filter(OperationLog.id == log_id).delete()
        if task_id:
            db.query(Task).filter(Task.id == task_id).delete()
        if provider_id:
            db.query(ModelProvider).filter(ModelProvider.id == provider_id).delete()
        # 注册默认初始化（4 任务 + 参考文档）随用户产生，按用户清干净才能删 user 行
        other_row = db.query(User).filter(User.username == other).first()
        for uid in (user_id, other_row.id if other_row else None):
            if not uid:
                continue
            db.query(Task).filter(Task.user_id == uid).delete()
            for d in db.query(SpecDoc).filter(SpecDoc.user_id == uid).all():
                db.query(SpecChunk).filter(SpecChunk.doc_id == d.id).delete()
                db.delete(d)
        db.query(User).filter(User.id == user_id).delete()
        db.query(User).filter(User.username == other).delete()
        db.commit()


# ---------- 收集需要在 finally 里清理的 ID ----------
user_id = None
provider_id = None
task_id = None
log_id = None
other = f"other_{SUFFIX}"

# 主体是否抛过异常。必须单独记：见下面 except / finally 的说明
CRASHED = False

try:
    # ---------- 0. Health ----------
    section("0. 健康检查")
    r = CLIENT.get(f"{BASE}/api/health")
    check("GET /api/health 200", r.status_code == 200, f"got {r.status_code}")
    check("database=connected", r.json().get("database") == "connected", str(r.json()))

    # ---------- 1. 注册 + 登录 ----------
    section("1. 注册 + 登录")
    r = CLIENT.post(
        f"{API}/auth/register",
        json={"username": USERNAME, "password": PASSWORD},
    )
    check("register 201", r.status_code == 201, f"{r.status_code} {r.text}")
    user_id = r.json().get("id") if r.status_code == 201 else None

    r = CLIENT.post(
        f"{API}/auth/register",
        json={"username": USERNAME, "password": PASSWORD},
    )
    check("重复注册 400", r.status_code == 400, f"got {r.status_code}")

    r = CLIENT.post(
        f"{API}/auth/register",
        json={"username": "ab", "password": "short"},  # 用户名太短、密码太短
    )
    check("Pydantic 校验非法参数 422", r.status_code == 422, f"got {r.status_code}")

    r = CLIENT.post(
        f"{API}/auth/login",
        json={"username": USERNAME, "password": "wrong_pw"},
    )
    check("错密码 401", r.status_code == 401, f"got {r.status_code}")

    r = CLIENT.post(
        f"{API}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    check("登录成功 200", r.status_code == 200, f"got {r.status_code} {r.text}")
    token = r.json()["access_token"]
    AUTH = {"Authorization": f"Bearer {token}"}
    require_private_providers(CLIENT, API, AUTH)  # 假端点在 127.0.0.1，先确认 SSRF 闸已放行

    # ---------- 2. 认证保护 ----------
    section("2. 未认证访问受保护接口")
    r = CLIENT.get(f"{API}/providers")
    check("无 token 401/403", r.status_code in (401, 403), f"got {r.status_code}")

    r = CLIENT.get(
        f"{API}/providers",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    check("假 token 401", r.status_code == 401, f"got {r.status_code}")

    # ---------- 3. Provider CRUD ----------
    section("3. 提供商 CRUD")
    r = CLIENT.post(
        f"{API}/providers",
        headers=AUTH,
        json={
            "name": f"fake-{SUFFIX}",
            "base_url": "http://127.0.0.1:9/v1",  # 不存在的地址
            "api_key": "sk-test-abcdefg1234567",
            "model_name": "fake-model",
        },
    )
    check("POST provider 201", r.status_code == 201, f"{r.status_code} {r.text}")
    provider_id = r.json().get("id") if r.status_code == 201 else None
    check("api_key 已脱敏", r.json().get("api_key_masked", "").endswith("****"))

    r = CLIENT.get(f"{API}/providers", headers=AUTH)
    check(
        "GET providers 200 且包含刚才新建",
        r.status_code == 200 and any(p["id"] == provider_id for p in r.json()),
    )

    r = CLIENT.put(
        f"{API}/providers/{provider_id}",
        headers=AUTH,
        json={"temperature": 0.3},
    )
    check("PUT provider 200", r.status_code == 200 and r.json()["temperature"] == 0.3, r.text)

    # ---------- 4. Task CRUD ----------
    section("4. 任务 CRUD")
    r = CLIENT.post(
        f"{API}/tasks",
        headers=AUTH,
        json={
            "name": "文本润色",
            "description": "把文字改得更流畅",
            "prompt_template": "请润色以下文本，只返回结果：\n\n{{content}}",
            "is_grading": False,
            "provider_id": provider_id,
        },
    )
    check("POST task 201", r.status_code == 201, f"{r.status_code} {r.text}")
    task_id = r.json().get("id") if r.status_code == 201 else None

    r = CLIENT.get(f"{API}/tasks", headers=AUTH)
    check("GET tasks 含刚建", r.status_code == 200 and any(t["id"] == task_id for t in r.json()))

    r = CLIENT.put(
        f"{API}/tasks/{task_id}",
        headers=AUTH,
        json={"is_grading": True},
    )
    ok = r.status_code == 200 and r.json()["is_grading"] is True
    check("PUT task 200 (改为批改)", ok, r.text)

    # ---------- 5. 数据隔离 ----------
    section("5. 数据隔离（另一用户看不到 task_id）")
    CLIENT.post(f"{API}/auth/register", json={"username": other, "password": PASSWORD})
    r = CLIENT.post(f"{API}/auth/login", json={"username": other, "password": PASSWORD})
    other_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = CLIENT.get(f"{API}/tasks", headers=other_auth)
    check("其他用户 GET tasks 不含别人任务", all(t["id"] != task_id for t in r.json()))
    r = CLIENT.put(f"{API}/tasks/{task_id}", headers=other_auth, json={"name": "hijack"})
    check("其他用户 PUT 别人 task 404", r.status_code == 404, f"got {r.status_code}")

    # ---------- 6. 上传 Word ----------
    section("6. 上传 .docx")
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("这是一个测试文档。")
    doc.add_paragraph("他高兴的跳了起来。")  # 故意留"的/地"错误
    doc.save(buf)
    buf.seek(0)

    r = CLIENT.post(
        f"{API}/upload?task_id={task_id}",
        headers=AUTH,
        files={
            "file": (
                "sample.docx",
                buf.read(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    check("POST /upload 201", r.status_code == 201, f"{r.status_code} {r.text}")
    log_id = r.json().get("log_id") if r.status_code == 201 else None
    check("状态 pending", r.json().get("status") == "pending")

    # 非法扩展名
    r = CLIENT.post(
        f"{API}/upload?task_id={task_id}",
        headers=AUTH,
        files={"file": ("bad.txt", b"hello", "text/plain")},
    )
    check("非 .docx 拒绝 400", r.status_code == 400, f"got {r.status_code}")

    # ---------- 7. 处理（预期失败，因为 provider 是假的） ----------
    section("7. LLM 处理（后台执行，预期失败但优雅）")
    r = CLIENT.post(f"{API}/process/{log_id}", headers=AUTH)
    check("POST /process 200", r.status_code == 200, f"{r.status_code} {r.text}")
    check("立即返回 processing", r.json().get("status") == "processing", r.text)

    data = {}
    misses = 0
    for _ in range(30):
        time.sleep(1)
        try:
            data = CLIENT.get(f"{API}/process/{log_id}/status", headers=AUTH).json()
            misses = 0
        except (httpx.HTTPError, ValueError) as e:
            # 单次轮询抖动不该把整套 e2e 打断。实测过一次 httpcore 读超时直接让脚本
            # 在第 7 节抛出、后面 24 项断言全没跑，而退出码问题修好后它至少会报错——
            # 但"一次网络抖动 = 整套失败"本身就是假的回归信号。连续 3 次才算真坏了。
            misses += 1
            print(f"  [warn] status 轮询第 {misses} 次失败：{type(e).__name__}")
            if misses >= 3:
                raise
        if data.get("status") in ("done", "failed"):
            break
    check("轮询到失败状态 failed", data.get("status") == "failed", str(data))
    check("有 error_message", bool(data.get("error_message")), str(data))

    # ---------- 8. 操作日志 ----------
    section("8. 操作日志")
    r = CLIENT.get(f"{API}/logs", headers=AUTH)
    check("GET /logs 200", r.status_code == 200)
    body = r.json()
    check("total >= 1", body.get("total", 0) >= 1, str(body))
    check("分页字段", "page" in body and "page_size" in body and "items" in body)

    # ---------- 10. 下载：直接改 DB 状态绕过 LLM，验证 diff + plain 两条路径 ----------
    section("10. 下载（模拟已完成，验证 Word 生成）")
    import database
    from models.operation_log import LogStatus as _LS
    from models.operation_log import OperationLog as _OL

    with database.SessionLocal() as session:
        log_row = session.query(_OL).filter(_OL.id == log_id).first()
        log_row.processed_content = "这是一个测试文档。\n他高兴地跳了起来。"
        log_row.status = _LS.DONE
        session.commit()

    r = CLIENT.get(f"{API}/download/{log_id}", headers=AUTH)
    check("批改任务下载 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("Content-Type 是 docx", "officedocument" in r.headers.get("content-type", ""))
    if r.status_code == 200:
        parsed = Document(io.BytesIO(r.content))
        text = "\n".join(p.text for p in parsed.paragraphs)
        check("下载的 docx 能解析且含内容", "测试" in text or "高兴" in text, f"got {text[:100]}")

    # ---------- 汇总 ----------
    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")

except Exception:
    # 主体崩了必须说出来。以前这里没有 except，异常一路冲进 finally，
    # 而 finally 里的 sys.exit 会把它连同 traceback 一起替换掉——于是
    # "半途而废的 e2e" 既不打错误、退出码还是 0，看起来跟全绿一模一样。
    # 实测这样藏了两轮才被发现，别再让它有机会沉默。
    traceback.print_exc()
    CRASHED = True

finally:
    section("清理")
    try:
        _cleanup_db()
        print(f"  已清理 DB 记录（user={user_id}, other={other}）")
    except Exception as e:
        print(f"  清理失败: {e}")
    sys.exit(4 if e2e_support.BLOCKED else (0 if (FAIL == 0 and not CRASHED) else 1))
