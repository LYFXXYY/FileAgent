"""
假 MinerU 云端点 —— 扫描件 OCR 兜底端到端用。

用法：cd server && uv run uvicorn test_mineru_server:app --port 19002
配合 test_e2e_mineru.py 使用，全程不碰真实 MinerU token、不产生页费用。

协议镜像官方云 v4 batch（2026-11-20 核实）：
  POST /file-urls/batch → PUT 预签名地址 → GET /extract-results/batch/{id} → 下载 zip
业务码约定也照抄：HTTP 200 但 code 非 0 表示业务错误（token 无效走这条）。

运行期开关（e2e 脚本通过 /_mock/mode 切换，不用重启进程）：
  done（默认）/ failed / no-zip-url / bad-zip / defective（片2：坏块夹具）
环境变量：
  MOCK_MINERU_TOKEN          期望的 Bearer token，默认 e2e-mineru-token
  MOCK_MINERU_RUNNING_POLLS  done 之前回几次 running，默认 1
  MOCK_MINERU_PUBLIC_BASE    预签名/下载 URL 的前缀，默认 http://127.0.0.1:19002
"""

import base64
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


# 合法 4×4 PNG（文件名仍叫 a.jpg，镜像真实云"扩展名与内容不一定一致"的产物）。
# 假 jpeg 字节会让 python-pptx 的 add_picture 直接炸，PPT e2e 需要真图
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAACXBIWXMAAA7EAAAOxAGVKw4bAAAANElEQVR4nGNwsYopqCzJyM+LMdZjMDSKcfHxKY6JT0lNZsjMTczRK6hMsQLyGJJBPAYIAACrjQ61AggBOQAAAABJRU5ErkJggg=="
)


def _text_block(kind: str, text: str, bbox: list, **extra) -> dict:
    content_key = f"{kind}_content"
    return {
        "type": kind,
        "content": {content_key: [{"type": "text", "content": text}], **extra},
        "bbox": bbox,
    }


# v2 页嵌套 + bbox 归一化 0..1000（2026-11-21 真实云探明的形状）：
# 第 0 页纵向文档（标题/正文/图/表 → PPT 走 B 流式重排），
# 第 1 页横向幻灯导出件（页眉即页标题 → PPT 走 A 整页忠实）
_V2_PAGES = [
    [
        _text_block("title", "扫描件标题", [100, 60, 900, 120], level=1),
        _text_block("paragraph", "这是一篇测试作文。", [100, 150, 900, 200]),
        _text_block("paragraph", "他高兴的跳了起来。", [100, 220, 900, 270]),
        {
            "type": "image",
            "content": {
                "image_source": {"path": "images/a.jpg"},
                "image_caption": [],
                "image_footnote": [],
            },
            "bbox": [100, 300, 500, 500],
        },
        {
            "type": "table",
            "content": {
                "html": (
                    "<table><tr><td rowspan=1 colspan=1>列A</td>"
                    "<td rowspan=1 colspan=1>列B</td></tr>"
                    "<tr><td rowspan=1 colspan=1>1</td>"
                    "<td rowspan=1 colspan=1>2</td></tr></table>"
                ),
                "image_source": {"path": "images/a.jpg"},
                "table_caption": [],
                "table_footnote": [],
            },
            "bbox": [100, 520, 900, 620],
        },
        _text_block("paragraph", "结尾段。", [100, 650, 900, 700]),
    ],
    [
        _text_block("page_header", "横向页标题", [0, 0, 1000, 80]),
        _text_block("paragraph", "横向页正文。", [100, 300, 900, 400]),
        _text_block("page_number", "2", [950, 950, 990, 990]),
    ],
]

_LAYOUT = {
    "pdf_info": [
        {"page_idx": 0, "page_size": [595, 842]},
        {"page_idx": 1, "page_size": [842, 474]},
    ]
}

# 片2 坏块修复 e2e 用：空文本段落 + 无 html 表格（都是确定性坏块判据），
# 外加一个正常块证明修复不误伤
_DEFECTIVE_PAGES = [
    [
        _text_block("title", "缺陷样本", [100, 60, 900, 120], level=1),
        _text_block("paragraph", "", [100, 150, 900, 200]),
        {
            "type": "table",
            "content": {
                "image_source": {"path": "images/a.jpg"},
                "table_caption": [],
                "table_footnote": [],
            },
            "bbox": [100, 220, 900, 400],
        },
        _text_block("paragraph", "正常段落不受影响。", [100, 420, 900, 470]),
    ],
]


def _zip_bytes(with_md: bool = True, defective: bool = False) -> bytes:
    # 命名镜像真实云产物（2026-11-20 实测）：content_list 带任务 UUID 前缀，
    # 且有 v2 变体——解包层按后缀匹配、v2 优先，e2e 顺带回归这条路
    uuid = "mock0000-0000-0000-0000-000000000000"
    pages = _DEFECTIVE_PAGES if defective else _V2_PAGES
    layout = {"pdf_info": _LAYOUT["pdf_info"][: len(pages)]} if defective else _LAYOUT
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
        zf.writestr(f"{uuid}_content_list_v2.json", json.dumps(pages, ensure_ascii=False))
        zf.writestr("layout.json", json.dumps(layout))
        zf.writestr("images/a.jpg", _PNG)
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
    mode = _MODE["mode"]
    return Response(
        content=_zip_bytes(with_md=mode != "bad-zip", defective=mode == "defective"),
        media_type="application/zip",
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
