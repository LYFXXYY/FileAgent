"""
假 VLM 端点 —— 阶段 5 视觉批评循环 e2e 用。

用法：cd server && uv run uvicorn test_vlm_server:app --port 19001

它**模拟一个 VLM**：接收含 image_url 的 messages、返回结构化批评意见。
和 test_llm_server 同形（FastAPI + OpenAI 兼容），但语义不同：

1. **看 prompt 里的 thread_id**：批评循环每页会带 `[critic-thread:log-X/round-N]` 元信息，
   mock 按这个 key 计数；前 MOCK_VLM_STUB_ROUNDS 次返回"有问题"，第 N+1 次返回"通过"。
2. **不解析图像字节**：mock 只看文本里的元信息，图像 URL 当装饰。阶段 5 e2e 的 PPTX
   渲染走 stub（§9.12.3 s5.4），图像内容是占位 PNG。
3. **response_format 行为**：与 test_llm_server 一致，json_schema/json_object 一律 400，
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


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-vlm"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """批评意见 mock。详见模块顶部 docstring"""
    body = await request.json()
    fmt = body.get("response_format")

    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        return JSONResponse(status_code=400, content={"error": "response_format is not supported"})

    messages = body.get("messages", [])
    # 拼所有文本部分（OpenAI 多模态格式：content 是 [{type:text|image_url,...}] 数组）
    text_blob = _extract_text(messages)

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
    return {"ok": True}
