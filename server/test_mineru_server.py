"""
假 MinerU 云端点 —— 扫描件 OCR 兜底端到端用。

用法：cd server && uv run uvicorn test_mineru_server:app --port 19002
配合 test_e2e_mineru.py 使用，全程不碰真实 MinerU token、不产生页费用。

协议镜像官方云 v4 batch（2026-11-20 核实）：
  POST /file-urls/batch → PUT 预签名地址 → GET /extract-results/batch/{id} → 下载 zip
业务码约定也照抄：HTTP 200 但 code 非 0 表示业务错误（token 无效走这条）。

运行期开关（e2e 脚本通过 /_mock/mode 切换，不用重启进程）：
  done（默认）/ failed / no-zip-url / bad-zip
环境变量：
  MOCK_MINERU_TOKEN          期望的 Bearer token，默认 e2e-mineru-token
  MOCK_MINERU_RUNNING_POLLS  done 之前回几次 running，默认 1
  MOCK_MINERU_PUBLIC_BASE    预签名/下载 URL 的前缀，默认 http://127.0.0.1:19002
"""

import io
import itertools
import json
import os
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="FileAgent Mock MinerU")

TOKEN = os.environ.get("MOCK_MINERU_TOKEN", "e2e-mineru-token")
RUNNING_POLLS = int(os.environ.get("MOCK_MINERU_RUNNING_POLLS", "1"))
PUBLIC_BASE = os.environ.get("MOCK_MINERU_PUBLIC_BASE", "http://127.0.0.1:19002").rstrip("/")

_SEQ = itertools.count(1)
_BATCHES: dict[str, dict] = {}
_MODE = {"mode": "done"}

# 产物 markdown：含标题 / 错别字句 / 图片 / 表格，
# 归一化层该去什么、该留什么，e2e 直接对着它断言
_MARKDOWN = """# 扫描件标题

这是一篇测试作文。

他高兴的跳了起来。

![插图](images/a.jpg)

| 列A | 列B |
| --- | --- |
| 1 | 2 |

结尾段。
"""


def _ok(data: dict) -> JSONResponse:
    return JSONResponse({"code": 0, "msg": "ok", "data": data})


def _zip_bytes(with_md: bool = True) -> bytes:
    # 命名镜像真实云产物（2026-11-20 实测）：content_list 带任务 UUID 前缀，
    # 且有 v2 变体——解包层按后缀匹配、v2 优先，e2e 顺带回归这条路
    uuid = "mock0000-0000-0000-0000-000000000000"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_md:
            zf.writestr("full.md", _MARKDOWN)
        zf.writestr(
            f"{uuid}_content_list.json",
            json.dumps(
                [{"type": "text", "text": "他高兴的跳了起来。", "page_idx": 0}], ensure_ascii=False
            ),
        )
        zf.writestr(
            f"{uuid}_content_list_v2.json",
            json.dumps(
                [
                    {
                        "type": "text",
                        "text": "他高兴的跳了起来。",
                        "page_idx": 0,
                        "bbox": [72, 300, 400, 320],
                    }
                ],
                ensure_ascii=False,
            ),
        )
        zf.writestr("images/a.jpg", b"\xff\xd8fake-jpeg-bytes")
    return buf.getvalue()


@app.post("/file-urls/batch")
async def file_urls_batch(request: Request):
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {TOKEN}":
        # 官方行为：HTTP 200 + 业务码报错，而不是 401
        return JSONResponse({"code": 401, "msg": "token 无效或已过期"})
    body = await request.json()
    batch_id = f"mock-b{next(_SEQ)}"
    _BATCHES[batch_id] = {"request": body, "uploaded": None, "put_headers": {}, "polls": 0}
    return _ok({"batch_id": batch_id, "file_urls": [f"{PUBLIC_BASE}/upload/{batch_id}"]})


@app.put("/upload/{batch_id}")
async def upload(batch_id: str, request: Request):
    if batch_id not in _BATCHES:
        return Response(status_code=404)
    _BATCHES[batch_id]["uploaded"] = await request.body()
    _BATCHES[batch_id]["put_headers"] = dict(request.headers)
    return Response(status_code=200)


@app.get("/extract-results/batch/{batch_id}")
async def extract_results(batch_id: str):
    batch = _BATCHES.get(batch_id)
    if batch is None:
        return JSONResponse({"code": 404, "msg": "batch 不存在"})
    if batch["uploaded"] is None:
        return _ok({"extract_result": [{"state": "waiting-file"}]})
    batch["polls"] += 1
    if batch["polls"] <= RUNNING_POLLS:
        return _ok({"extract_result": [{"state": "running"}]})
    mode = _MODE["mode"]
    if mode == "failed":
        return _ok({"extract_result": [{"state": "failed", "err_msg": "mock 解析失败"}]})
    item: dict = {"state": "done"}
    if mode != "no-zip-url":
        item["full_zip_url"] = f"{PUBLIC_BASE}/zip/{batch_id}"
    return _ok({"extract_result": [item]})


@app.get("/zip/{batch_id}")
async def download_zip(batch_id: str):
    if batch_id not in _BATCHES:
        return Response(status_code=404)
    return Response(
        content=_zip_bytes(with_md=_MODE["mode"] != "bad-zip"), media_type="application/zip"
    )


@app.post("/_mock/reset")
async def reset():
    _BATCHES.clear()
    _MODE["mode"] = "done"
    return {"ok": True}


@app.post("/_mock/mode")
async def set_mode(request: Request):
    body = await request.json()
    _MODE["mode"] = body.get("mode", "done")
    return {"ok": True, "mode": _MODE["mode"]}


@app.get("/_mock/last")
async def last():
    """回吐最后一个 batch 的申请体 / 上传字节数 / PUT 头，供 e2e 断言请求形状"""
    if not _BATCHES:
        return {"batch": None}
    batch_id = list(_BATCHES)[-1]
    batch = _BATCHES[batch_id]
    return {
        "batch": {
            "batch_id": batch_id,
            "request": batch["request"],
            "uploaded_len": len(batch["uploaded"]) if batch["uploaded"] is not None else None,
            "put_content_type": batch["put_headers"].get("content-type"),
            "polls": batch["polls"],
        }
    }
