"""
假 OpenAI 兼容端点 —— 结构化批改端到端 + 阶段 5 PPT 大纲生成端到端用。

用法：cd server && uv run uvicorn test_llm_server:app --port 19000
配合 test_e2e_structural.py / test_e2e_ppt.py 使用，全程不碰真实 API Key、不产生 token 费用。

按 system prompt 内容路由响应形状：
- "幻灯片大纲" → 返回 PptOutline JSON
- 其他（默认）→ 返回 GradingResult JSON
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FileAgent Mock LLM")


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-text"}]}


def _outline_payload() -> str:
    return json.dumps(
        {
            "title": "演示文稿",
            "slides": [
                {
                    "title": "封面",
                    "bullets": ["文档要点 1", "文档要点 2"],
                    "image_placeholders": [],
                },
                {
                    "title": "正文",
                    "bullets": ["论据 A", "论据 B", "论据 C"],
                    "image_placeholders": [],
                },
                {"title": "总结", "bullets": ["回顾"], "image_placeholders": []},
            ],
        },
        ensure_ascii=False,
    )


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


def _is_outline_request(messages: list[dict]) -> bool:
    """
    system prompt 含 plan / revise 的专有短语 → 大纲类请求

    - plan_slides 走 services/ppt_outline.OUTLINE_OUTPUT_INSTRUCTION（含"幻灯片大纲"）
    - revise_outline 走 services/vlm_critic.REVISE_SYSTEM（含"完整新大纲"）
    - critique 走 CRITIC_SYSTEM 也含"大纲"二字，但 mock VLM 端点 19001 单独处理这个；
      本服务器只接 19000 上的文本外呼，撞不到 critique 请求
    """
    markers = ("幻灯片大纲", "完整新大纲")
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, str) and any(k in content for k in markers):
                return True
    return False


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    模拟 OpenAI 兼容响应。

    刻意不支持 response_format（国产端点常见行为）：收到 json_schema / json_object
    时返回 400，逼 chat_json 走降级路径；同时返回固定 JSON 串，
    便于 e2e 脚本断言解析定位结果。
    """
    body = await request.json()
    fmt = body.get("response_format")

    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        return JSONResponse(status_code=400, content={"error": "response_format is not supported"})

    messages = body.get("messages", [])
    if _is_outline_request(messages):
        content = _outline_payload()
    else:
        prompt = messages[-1].get("content", "") if messages else ""
        content = _grading_payload(prompt)

    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": "answered-model",
        "usage": {"prompt_tokens": 1234, "completion_tokens": 234, "total_tokens": 1468},
    }
