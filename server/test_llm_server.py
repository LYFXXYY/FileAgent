"""
假 OpenAI 兼容端点 —— 结构化批改类端到端用。

用法：cd server && uv run uvicorn test_llm_server:app --port 19000
配合 test_e2e_structural.py / test_e2e_mineru.py / test_e2e_rag.py 使用，
全程不碰真实 API Key、不产生 token 费用。

响应形状只有一种：GradingResult JSON。阶段 5 那套"幻灯片大纲"分支已随链路整体删除
（AGENTS §3.11），别再往回加——现役五套 e2e 没有任何请求需要它。
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FileAgent Mock LLM")

# 最后一次收到的 messages。RAG 端到端靠它证明"条款真的进了 prompt"
_LAST: dict = {"messages": []}


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-text"}]}


def _grading_payload(prompt: str) -> str:
    return json.dumps(
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    模拟 OpenAI 兼容响应。

    刻意不支持 response_format（国产端点常见行为）：收到 json_schema / json_object
    时返回 400，逼 chat_json 走降级路径；同时返回固定 JSON 串，
    便于 e2e 脚本断言解析定位结果。
    """
    body = await request.json()
    _LAST["messages"] = body.get("messages", [])
    fmt = body.get("response_format")

    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        return JSONResponse(status_code=400, content={"error": "response_format is not supported"})

    messages = body.get("messages", [])
    prompt = messages[-1].get("content", "") if messages else ""
    content = _grading_payload(prompt)

    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": "answered-model",
        "usage": {"prompt_tokens": 1234, "completion_tokens": 234, "total_tokens": 1468},
    }


@app.post("/v1/_mock/reset")
async def _reset():
    """清空记录，避免跨用例污染（与 test_vlm_server 同一套约定）"""
    _LAST["messages"] = []
    return {"ok": True}


@app.get("/v1/_mock/last")
async def _last():
    """
    回吐最后一次收到的 messages。

    给 RAG 端到端用：条款到底有没有进 prompt，只有被调用方自己能证明；
    看结果里的 refs 只能说明"检索到了"，不能说明"送进去了"。
    """
    return {"messages": _LAST["messages"]}
