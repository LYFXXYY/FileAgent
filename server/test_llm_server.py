"""
假 OpenAI 兼容端点 —— 结构化批改端到端测试用。

用法：cd server && uv run uvicorn test_llm_server:app --port 19000
配合 test_e2e_structural.py 使用，全程不碰真实 API Key、不产生 token 费用。
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FileAgent Mock LLM")


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-text"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    模拟 OpenAI 兼容响应。

    刻意不支持 response_format（国产端点常见行为）：收到 json_schema / json_object
    时返回 400，逼 chat_json 走降级路径；同时返回固定 JSON 串，
    便于 test_e2e_structural.py 断言解析定位结果。
    """
    body = await request.json()
    fmt = body.get("response_format")

    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        return JSONResponse(status_code=400, content={"error": "response_format is not supported"})

    messages = body.get("messages", [])
    prompt = messages[-1].get("content", "") if messages else ""
    content = json.dumps(
        {
            "revised_text": f"{prompt}\n\n（修订后全文：他高兴地跳了起来）",
            "issues": [
                {
                    "type": "typo",
                    "original": "高兴的跳了起来",
                    "suggestion": "高兴地跳了起来",
                    "reason": "的地不分",
                    "confidence": 0.92,
                }
            ],
        },
        ensure_ascii=False,
    )

    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": "answered-model",
        "usage": {"prompt_tokens": 1234, "completion_tokens": 234, "total_tokens": 1468},
    }
