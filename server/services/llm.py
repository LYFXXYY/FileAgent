"""
LLM 调用服务 —— 通用 OpenAI 兼容接口

【设计思路】
- 豆包 / 通义 / GPT / DeepSeek / Ollama 都提供 OpenAI 兼容的 /chat/completions 端点
- 所以只需要一套 base_url + api_key + model_name 就能对接任意模型
- 换模型 = 改配置，代码不动

【Java 对比】
- httpx.AsyncClient ≈ WebClient (Spring WebFlux) 或 RestTemplate
- async/await 让 IO 等待期间不阻塞事件循环（比 Java 传统同步 HttpClient 高效）
"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from models.provider import ModelProvider


class LLMError(Exception):
    """LLM 调用失败时抛出的统一异常，让上层路由捕获后写入 error_message"""


async def call_llm(
    db: Session,
    prompt: str,
    provider: ModelProvider | None = None,
    user_id: int | None = None,
) -> str:
    """
    调用 LLM 并返回文本结果

    provider=None 时自动选一个 is_active=True 的提供商（限定在 user_id 名下，按 id 升序）
    user_id 用于隔离：只在该用户的提供商里挑默认的，不会用到别人的 Key
    """
    # 未指定 provider → 选一个默认的（限定当前用户）
    if provider is None:
        q = db.query(ModelProvider).filter(ModelProvider.is_active == True)  # noqa: E712
        if user_id is not None:
            q = q.filter(ModelProvider.user_id == user_id)
        provider = q.order_by(ModelProvider.id).first()
        if provider is None:
            raise LLMError("系统中没有任何可用的模型提供商，请先添加")

    # OpenAI 兼容接口：POST {base_url}/chat/completions
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": provider.model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": provider.max_tokens,
        "temperature": provider.temperature,
    }

    # httpx.AsyncClient: 异步 HTTP 客户端
    # timeout=120：LLM 响应可能很慢，尤其长文本
    # 类比 Java 的 WebClient.create() + .timeout(Duration)
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise LLMError(f"LLM 请求超时：{e}") from e
    except httpx.RequestError as e:
        raise LLMError(f"LLM 网络错误：{e}") from e

    if resp.status_code != 200:
        # 抛出时对响应体截断，防止 API Key 等敏感信息泄漏到错误消息
        raise LLMError(f"LLM 返回 {resp.status_code}：{resp.text[:500]}")

    try:
        data = resp.json()
        # OpenAI 响应结构：choices[0].message.content
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f"LLM 响应格式异常：{e}, body={resp.text[:500]}") from e

    return content
