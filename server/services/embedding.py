"""规范库向量编码（阶段 6）—— 本地 OpenAI 兼容 embedding 端点的传输层

【为什么这一层独立，不复用 services/llm.py】
llm.py 管的是"生成"：重试白名单、三级降级、并发闸、token 计量，全为聊天端点服务。
embedding 端点一个都不需要——它没有格式契约要降级、没有 completion token 可记、
请求体一样就结果一样。把两套塞进一个模块，换来的是批改的重试逻辑被编码请求共用，
排查时分不清是谁在重试。

【三个已实测的静默坑，都在这里挡住】
1. **查询前缀只能应用侧拼，而且加不加由模型决定**：GGUF 元数据里没有任何前缀信息，
   配置给了字符串就得由这里拼上去。bge-m3 实测**不该加**（9 条夹具、k=5：加了
   recall@5=0.380，不加 0.491）——它训练时不带指令，与 bge-large-zh 那一批不同。
   加了不会报错，只会让相似度整体漂移，所以默认值是空串而不是那句常用前缀。
2. **超长输入静默截断**：16,280 字仍返回 1024 维，与截断到 1200 字的余弦只有 0.8811。
   端点不报错，所以长度得自己控（检索侧靠 services/spec_retrieval.plan_windows 滑窗）。
3. **usage.prompt_tokens 恒 0**：拿不到真实 token，成本无法从响应得到。
   所以本模块**不往 llm_calls 写任何行**——写进去的就是编造的数字。
   要量成本用字符数代理：`sum(len(chunk.text))`，库里本来就有。

【为什么是同步函数】
入库路由是 `def`（FastAPI 丢线程池），在这里阻塞最自然；异步侧（检索注入）要用的话
由调用方 `run_in_threadpool` 包一层，与 §3.3 里 `record_llm_call()` 同一个套路。
反过来做成 async，同步的入库路径就得自己 asyncio.run，那是把线程池的活抢回来自己干。

【失败语义】
配置缺失、维度不符、条数对不上、连不上——一律 EmbeddingError，**不重试也不降级**。
编码是幂等的，重跑比在库里留一个半成品便宜。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from config import get_settings
from models.spec_doc import EMBEDDING_DIM

logger = logging.getLogger("fileagent.embedding")

# 单次请求的文本条数。实测 66 条一批 2.17s，比逐条快 1.6 倍，所以批量是对的；
# 但整库回填可能上千块，一批发出去请求体太大，按 32 条切段
_BATCH_SIZE = 32

# 进程级共享客户端：复用连接池。与 llm.py 的 get_client() 分开，因为两者的
# trust_env 取值相反（见 _get_client 里的说明）
_client: httpx.Client | None = None


class EmbeddingError(Exception):
    """编码失败。message 是给前端/运维看的中文，不外抛堆栈"""


def _check_config() -> tuple[str, str, float]:
    """
    取端点三要素并校验维度声明。

    base_url / model 刻意没有默认值：本机 LM Studio 的端口随启动漂移，
    写死一个"看着能用"的默认值，换来的是静默打到别的模型上。
    """
    settings = get_settings()
    base_url = (settings.embedding_base_url or "").rstrip("/")
    model = (settings.embedding_model or "").strip()
    if not base_url or not model:
        raise EmbeddingError(
            "embedding 端点未配置。请在 server/.env 填 EMBEDDING_BASE_URL 与 "
            "EMBEDDING_MODEL（本地可用 LM Studio 起 bge-m3，端口以实际监听为准）"
        )
    if settings.embedding_dimensions != EMBEDDING_DIM:
        raise EmbeddingError(
            f"配置声明 {settings.embedding_dimensions} 维，但 spec_chunks.embedding "
            f"列宽是 {EMBEDDING_DIM} 维。两者必须一致，否则写入时整批报错；"
            "换 embedding 模型要同步改 EMBEDDING_DIMENSIONS、重建该列、重跑回填"
        )
    return base_url, model, settings.embedding_timeout


def _get_client() -> httpx.Client:
    """
    共享 httpx.Client。

    `trust_env=False` 是刻意的：端点固定在 127.0.0.1，走系统代理只会绕远，
    而且本机 NO_PROXY 含裸 `[::1]` 时 httpx 直接抛 InvalidURL: Invalid port
    （llm.py 那边这个开关是配置项，因为它的端点可能要翻代理）。
    """
    global _client
    if _client is None:
        _client = httpx.Client(trust_env=False, timeout=httpx.Timeout(60.0, connect=10.0))
    return _client


def close_client() -> None:
    """给测试和 lifespan 收尾用；不关也不影响进程退出"""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """
    文档侧编码：**不加前缀**。

    返回顺序与入参一致。任何一段响应的维度不是 EMBEDDING_DIM 都直接抛——
    宁可这批不入库，也不让两种维度的向量混在同一列里。
    """
    if not texts:
        return []

    base_url, model, timeout = _check_config()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = list(texts[start : start + _BATCH_SIZE])
        vectors.extend(_post_batch(base_url, model, batch, timeout))

    if len(vectors) != len(texts):
        raise EmbeddingError(f"编码条数对不上：请求 {len(texts)} 条，回来 {len(vectors)} 条")
    return vectors


def embed_query(text: str) -> list[float]:
    """
    查询侧编码：**加非对称前缀**（坑 1）。

    前缀只在这里拼。入库走 embed_texts，所以库里存的是"文档表示"、
    查询是"查询表示"，这正是 bge 检索预期的配对方式。
    """
    prefix = get_settings().embedding_query_prefix or ""
    vectors = embed_texts([prefix + text])
    return vectors[0]


def _post_batch(
    base_url: str,
    model: str,
    batch: list[str],
    timeout: float,
) -> list[list[float]]:
    url = f"{base_url}/embeddings"
    try:
        resp = _get_client().post(
            url,
            json={"model": model, "input": batch},
            headers=_auth_headers(),
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise EmbeddingError(f"连不上 embedding 端点 {url}：{_exc_text(e)}") from e

    if resp.status_code >= 400:
        # 截断响应体：端点的报错常把整个请求回显，太长且没信息量
        raise EmbeddingError(
            f"embedding 端点返回 {resp.status_code}：{resp.text[:300]}",
        )

    try:
        payload = resp.json()
        items = payload["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise EmbeddingError(f"embedding 响应不是预期结构：{e}") from e

    # OpenAI 约定 data[i].index 对应输入顺序；本地端点偶有乱序，按 index 归位
    ordered = sorted(items, key=lambda it: it.get("index", 0))
    vectors = [it["embedding"] for it in ordered]
    for vec in vectors:
        if len(vec) != EMBEDDING_DIM:
            raise EmbeddingError(
                f"模型 {model} 返回 {len(vec)} 维，与列宽 {EMBEDDING_DIM} 不符。"
                "多半是端点上挂了别的 embedding 模型（如 nomic=768），"
                "把 EMBEDDING_MODEL 改成实际用的那个"
            )

    logger.info("embedding 编码 %d 条（%d 字）", len(batch), sum(len(t) for t in batch))
    return vectors


def _auth_headers() -> dict[str, str]:
    key = (get_settings().embedding_api_key or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _exc_text(e: Exception) -> str:
    """异常详情为空时退到类名（httpx 在 asyncio 门下常 str() 为空，同 llm.py 的处理）"""
    return str(e) or type(e).__name__
