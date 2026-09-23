"""
后端端到端测试脚本 —— 覆盖认证、CRUD、上传、处理、日志

用法：cd server && uv run python test_e2e.py
"""

from __future__ import annotations

import io
import sys
import time

# 强制 stdout 用 UTF-8，避免 Windows 控制台 GBK 编码问题
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from docx import Document

BASE = "http://localhost:8000"
API = f"{BASE}/api/v1"
CLIENT = httpx.Client(timeout=30.0)

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
check("登录成功 200", r.status_code == 200, f"{r.status_code} {r.text}")
token = r.json()["access_token"]
AUTH = {"Authorization": f"Bearer {token}"}

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
check("PUT task 200 (改为批改)", r.status_code == 200 and r.json()["is_grading"] is True, r.text)

# ---------- 5. 数据隔离 ----------
section("5. 数据隔离（另一用户看不到 task_id）")
other = f"other_{SUFFIX}"
CLIENT.post(f"{API}/auth/register", json={"username": other, "password": PASSWORD})
r = CLIENT.post(f"{API}/auth/login", json={"username": other, "password": PASSWORD})
other_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = CLIENT.get(f"{API}/tasks", headers=other_auth)
check("其他用户 GET tasks 不含别人任务", all(t["id"] != task_id for t in r.json()))
r = CLIENT.put(f"{API}/tasks/{task_id}", headers=other_auth, json={"name": "hijack"})
check("其他用户 PUT 别人 task 404", r.status_code == 404, f"got {r.status_code}")

# ---------- 6. 上传 Word ----------
section("6. 上传 .docx")
# 构造一个小的 .docx 到内存
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
section("7. LLM 处理（预期失败但优雅）")
r = CLIENT.post(f"{API}/process/{log_id}", headers=AUTH)
check("POST /process 200", r.status_code == 200, f"{r.status_code} {r.text}")
data = r.json()
check("失败状态 failed", data.get("status") == "failed")
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
# 用数据库会话直接把刚才那条 log 改成 done + 塞入 processed_content
import database  # noqa: E402
from models.operation_log import LogStatus as _LS  # noqa: E402
from models.operation_log import OperationLog as _OL  # noqa: E402

with database.SessionLocal() as session:
    log_row = session.query(_OL).filter(_OL.id == log_id).first()
    log_row.processed_content = "这是一个测试文档。\n他高兴地跳了起来。"
    log_row.status = _LS.DONE
    session.commit()

# 该任务 is_grading=True → 走 diff 路径
r = CLIENT.get(f"{API}/download/{log_id}", headers=AUTH)
check("批改任务下载 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
check("Content-Type 是 docx", "officedocument" in r.headers.get("content-type", ""))
# 验证返回的确实是有效 .docx（能重新解析）
if r.status_code == 200:
    parsed = Document(io.BytesIO(r.content))
    text = "\n".join(p.text for p in parsed.paragraphs)
    check("下载的 docx 能解析且含内容", "测试" in text or "高兴" in text, f"got {text[:100]}")

# ---------- 11. 清理 ----------
section("11. 删除任务/提供商")
r = CLIENT.delete(f"{API}/tasks/{task_id}", headers=AUTH)
check("DELETE task 204", r.status_code == 204, f"got {r.status_code}")
r = CLIENT.delete(f"{API}/providers/{provider_id}", headers=AUTH)
check("DELETE provider 204", r.status_code == 204, f"got {r.status_code}")

# ---------- 汇总 ----------
print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")
sys.exit(0 if FAIL == 0 else 1)
