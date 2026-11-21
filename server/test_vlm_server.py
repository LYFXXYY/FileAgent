"""
假 VLM 端点 —— ② PPT 转换的片2 坏块修复 + 片3 版面批评循环 e2e 用。

用法：cd server && uv run uvicorn test_vlm_server:app --port 19001

它**模拟一个 VLM**：接收含 image_url 的 messages，按 prompt 里的标记分派：

1. **修复分支（片2）**：文本含 `[ppt-repair:log-N]` 时，按提示词是表格还是文字
   返回确定性的 `{"content": ...}` 产出（表格给合法 HTML、文字给固定转录文本），
   并计数供 e2e 断言"真的外呼过"。
2. **版面批评分支（片3）**：文本含 `[ppt-layout-thread:...]` 时按 thread 计数，
   前 MOCK_VLM_STUB_ROUNDS 次返回 needs_revision（点名幻灯片 1，category=stacked），
   之后返回 pass。issues 只含版面问题，与生产提示词"只评版面"同契约。
3. **历史批评分支（大纲流水线，已无现役消费方）**：`[critic-thread:...]` 计数逻辑保留。
4. **不解析图像字节**：mock 只看文本标记。真实 VLM 看的是图。
5. **response_format 行为**：与 test_llm_server 一致，json_schema/json_object 一律 400，
   逼 chat_json 走降级阶梯，验证传输层不依赖端点守约定。

MOCK_VLM_STUB_ROUNDS 默认 1：第一次外呼挑刺、第二次说没问题。
"""

import json
import os
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FileAgent Mock VLM")

_STUB_ROUNDS = int(os.environ.get("MOCK_VLM_STUB_ROUNDS", "1"))

# 进程内计数：(thread_id) → 已挑刺次数。重启清空，e2e 跑前后都干净
_calls_per_thread: dict[str, int] = defaultdict(int)
# 片2：修复外呼计数（按 log 标记分桶），e2e 用它证明"真的带着区域图打过 VLM"
_repair_calls: dict[str, int] = defaultdict(int)
# 片3：版面批评计数（按 thread 标记分桶）
_layout_calls: dict[str, int] = defaultdict(int)

_REPAIR_TABLE_HTML = (
    "<table><tr><td rowspan=1 colspan=1>修复列A</td>"
    "<td rowspan=1 colspan=1>修复列B</td></tr>"
    "<tr><td rowspan=1 colspan=1>1</td><td rowspan=1 colspan=1>2</td></tr></table>"
)
_REPAIR_TEXT = "VLM 修复的正文。"


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-vlm"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """修复 / 批评两种 mock。详见模块顶部 docstring"""
    body = await request.json()
    fmt = body.get("response_format")

    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        return JSONResponse(status_code=400, content={"error": "response_format is not supported"})

    messages = body.get("messages", [])
    # 拼所有文本部分（OpenAI 多模态格式：content 是 [{type:text|image_url,...}] 数组）
    text_blob = _extract_text(messages)

    # 片2 修复分支：必须收到 image_url（区域原图 grounding）才作答
    if "[ppt-repair:" in text_blob:
        if not _has_image(messages):
            return JSONResponse(
                status_code=400, content={"error": "repair request missing image_url"}
            )
        key = _parse_marker(text_blob, "[ppt-repair:") or "anonymous"
        _repair_calls[key] += 1
        content = _REPAIR_TABLE_HTML if "表格" in text_blob else _REPAIR_TEXT
        payload = json.dumps({"content": content}, ensure_ascii=False)
        return {
            "choices": [{"message": {"content": payload}, "finish_reason": "stop"}],
            "model": "answered-vlm-model",
            "usage": {"prompt_tokens": 1200, "completion_tokens": 60, "total_tokens": 1260},
        }

    # 片3 版面批评分支：前 MOCK_VLM_STUB_ROUNDS 次挑刺（点名幻灯片 1），之后 pass。
    # 只评版面——issues 里没有任何文字内容修改建议，与生产提示词同契约
    if "[ppt-layout-thread:" in text_blob:
        key = _parse_marker(text_blob, "[ppt-layout-thread:") or "anonymous"
        n = _layout_calls[key]
        _layout_calls[key] = n + 1
        if n >= _STUB_ROUNDS:
            payload = json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)
        else:
            payload = json.dumps(
                {
                    "verdict": "needs_revision",
                    "issues": [
                        {
                            "slide_index": 1,
                            "category": "stacked",
                            "description": "整页忠实页存在竖排堆叠（mock 反馈）",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return {
            "choices": [{"message": {"content": payload}, "finish_reason": "stop"}],
            "model": "answered-vlm-model",
            "usage": {"prompt_tokens": 3000, "completion_tokens": 80, "total_tokens": 3080},
        }

    thread_id = _parse_thread_id(text_blob) or "anonymous"
    round_count = _calls_per_thread[thread_id]
    _calls_per_thread[thread_id] = round_count + 1

    if round_count >= _STUB_ROUNDS:
        verdict = "pass"
        issues: list[dict] = []
    else:
        verdict = "needs_revision"
        issues = [
            {
                "slide_index": 1,
                "category": "title_overlap",
                "severity": "high",
                "description": "标题字号偏大压到正文框（mock 反馈）",
                "suggestion": "标题缩到 28pt 以内",
            }
        ]

    payload = json.dumps({"verdict": verdict, "issues": issues}, ensure_ascii=False)

    return {
        "choices": [{"message": {"content": payload}, "finish_reason": "stop"}],
        "model": "answered-vlm-model",
        "usage": {"prompt_tokens": 800, "completion_tokens": 120, "total_tokens": 920},
    }


def _extract_text(messages: list[dict]) -> str:
    """
    把 messages 里所有 text 部分拼起来

    OpenAI 兼容多模态格式：
        content = "字符串" 或 [{"type":"text","text":"..."}, {"type":"image_url",...}]
    """
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(str(piece.get("text", "")))
    return "\n".join(parts)


def _has_image(messages: list[dict]) -> bool:
    """修复必须带区域原图——没图的请求说明调用方在"裸猜"，直接 400 暴露"""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "image_url":
                    return True
    return False


def _parse_marker(text: str, marker: str) -> str | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    end = text.find("]", idx)
    if end < 0:
        return None
    return text[idx + len(marker) : end].strip()


def _parse_thread_id(text: str) -> str | None:
    """
    从 prompt 文本里抠 [critic-thread:...] 标记。批评图（s5.6）会注入这一行

    找不到标记时调用方走 anonymous 桶——e2e 至少要给标记，单测可以不给
    """
    marker = "[critic-thread:"
    idx = text.find(marker)
    if idx < 0:
        return None
    end = text.find("]", idx)
    if end < 0:
        return None
    return text[idx + len(marker) : end].strip()


@app.post("/v1/_mock/reset")
async def reset_state():
    """单测和 e2e 跑前清状态。生产链路不调这个"""
    _calls_per_thread.clear()
    _repair_calls.clear()
    _layout_calls.clear()
    return {"ok": True}


@app.get("/v1/_mock/repairs")
async def repair_stats():
    """片2/片3 e2e：回吐按标记分桶的修复与版面批评外呼次数"""
    return {"repairs": dict(_repair_calls), "layout": dict(_layout_calls)}
