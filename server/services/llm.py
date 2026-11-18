"""
LLM 调用服务 —— 通用 OpenAI 兼容接口的传输层

【设计思路】
- 豆包 / 通义 / GPT / DeepSeek / Ollama / LM Studio 都提供 OpenAI 兼容的
  /chat/completions 端点，所以一套 base_url + api_key + model_name 就能对接任意模型，
  换模型 = 改配置，代码不动
- 本模块只管"怎么把请求发出去、怎么把响应读回来"：多轮 messages、超时、重试、
  并发闸、结构化输出、计量。**不做业务编排**——拼 prompt、写回日志都在调用方
- chat() 不碰数据库，所以能用 mock 直接单测；需要 DB 的两件事拆成独立函数
  （resolve_provider 选模型、record_llm_call 记计量）

【Java 对比】
- httpx.AsyncClient ≈ WebClient（Spring WebFlux），共享实例相当于复用连接池的
  HttpClient Bean，而不是每次 request 新建
- asyncio.Semaphore ≈ 信号量 / 限流器（Guava Semaphore），控制同时在飞的请求数
- time.perf_counter ≈ System.nanoTime()，测耗时用它而不是 wall clock
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from typing import Any

import httpx
from sqlalchemy.orm import Session

from config import get_settings
from database import SessionLocal
from models.llm_call import LlmCall
from models.provider import ModelProvider
from models.user import User

logger = logging.getLogger("fileagent.llm")

# 值得重试的状态码：限流、网关抖动、服务端超时。
# 其他 4xx（400 参数错、401 key 无效）重试也还是错，重试纯烧 token
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Retry-After 超过这个秒数就不等了，交给调用方判超时
_MAX_RETRY_AFTER = 60.0
# 指数退避的等待上限（秒）
_BACKOFF_CAP = 20.0

# ---------- 共享 HTTP 客户端与并发闸 ----------
# 进程级单例：复用连接池，省掉每次调用的 DNS + TLS 握手
_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None


class LLMError(Exception):
    """
    LLM 调用最终失败（含重试耗尽）时抛出，上层捕获后写入 error_message

    attempts 记录实际发起过几次 HTTP 请求。失败路径也必须带上它，
    否则计量表里的 attempts 永远是 1，看不出"这次是重试到耗尽才失败"还是"一次就挂"。
    """

    def __init__(self, message: str, *, attempts: int = 1):
        super().__init__(message)
        self.attempts = attempts


class _Retryable(Exception):
    """内部信号：这次失败可以重试。不往外抛给调用方"""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class LLMResult:
    """一次成功外呼的完整结果，含计量信息，供调用方写库和评测台回放"""

    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    model_name: str
    finish_reason: str | None
    # 含首次尝试，1 表示一次成功；>1 说明中间碰到过可重试的失败
    attempts: int
    # chat_json 实际落在 response_format 阶梯的哪一档：json_schema / json_object / plain。
    # 纯 chat 走 None。跨端点比较必须知道这个，否则"分数差"分不清是模型差
    # 还是端点不支持严格档——评测台没有它就等于瞎评
    format_tier: str | None = None


def get_client() -> httpx.AsyncClient:
    """取共享客户端，首次调用时按配置创建；已关闭则自动重建"""
    global _client
    if _client is not None and not _client.is_closed:
        return _client

    s = get_settings()
    try:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(s.llm_timeout_read, connect=s.llm_timeout_connect),
            limits=httpx.Limits(
                max_connections=s.llm_max_concurrency,
                max_keepalive_connections=s.llm_max_concurrency,
            ),
            trust_env=s.llm_trust_env_proxy,
        )
    except Exception as e:
        # httpx 构造时会解析代理环境变量，NO_PROXY 里有非法写法（如裸 [::1]）就在这里炸
        raise LLMError(
            f"LLM HTTP 客户端初始化失败：{e}。若报 InvalidURL: Invalid port，"
            "通常是 NO_PROXY 含非法条目，把 LLM_TRUST_ENV_PROXY 设为 false 可绕开"
        ) from e
    return _client


async def close_client() -> None:
    """应用关闭时释放连接池（lifespan 调用）"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _get_semaphore() -> asyncio.Semaphore:
    """进程内并发闸。⚠ 多 worker 部署时每个进程各有一个，不是全局上限"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().llm_max_concurrency)
    return _semaphore


def parse_capabilities(raw: str | None) -> set[str]:
    """解析逗号分隔的能力标记；空值一律当作纯文本模型"""
    items = {c.strip().lower() for c in (raw or "").split(",") if c.strip()}
    return items or {"text"}


def provider_usable_by(provider: ModelProvider, user: User | None) -> bool:
    """
    单个提供商对某用户是否可用（SPEC §9.11 的授权判据，路由层共用）

    - 自己的 → 永远可用；
    - 公共（is_public）→ 仅 can_use_default=True 的用户可用；
    - 别人的私有 → 永远不可用。
    user 为 None（用户已被删）时只按"非公共即不可用"处理，调用方各自决定报错文案。
    """
    if user is not None and provider.user_id == user.id:
        return True
    return bool(provider.is_public and user is not None and user.can_use_default)


def resolve_provider(
    db: Session,
    user_id: int,
    provider_id: int | None = None,
    capability: str = "text",
) -> ModelProvider:
    """
    选出本次要用的提供商：任务显式指定 > 自己的默认 active > （被授权时）公共默认 active

    三条硬规则：
    - 指定的提供商必须"对该用户可用"（自己的，或被授权的公共提供商），否则**显式报错**，
      不静默换模型——换模型会改变输出，用户必须知道；
    - 自动挑选只在自己的 active 里找，找不到且 `can_use_default=True` 才回落公共池；
    - 用户没被授权时，公共提供商对他完全不存在（列表不展示、指定了也报错）。
    """
    user = db.get(User, user_id)

    if provider_id is not None:
        provider = db.get(ModelProvider, provider_id)
        # 自己的提供商直接放行；公共的要看授权。users 行缺失时仍认"归属即合法"，
        # 与单测内存库（只建 provider 不建 user）保持兼容
        usable = provider is not None and (
            provider.user_id == user_id or provider_usable_by(provider, user)
        )
        if not usable:
            raise LLMError("任务指定的模型提供商已被删除或不可用，请重新选择")
        if capability not in parse_capabilities(provider.capabilities):
            raise LLMError(f"任务指定的提供商 {provider.name} 不支持 {capability} 能力，请重新选择")
        return provider

    active = (
        db.query(ModelProvider)
        .filter(
            ModelProvider.user_id == user_id,
            ModelProvider.is_active == True,  # noqa: E712
        )
        .order_by(ModelProvider.id)
    )
    for provider in active:
        if capability in parse_capabilities(provider.capabilities):
            return provider

    # 自己一个不合用 → 被授权的用户回落公共池（注册即可用的"官方默认"路径）
    if user is not None and user.can_use_default:
        public = (
            db.query(ModelProvider)
            .filter(
                ModelProvider.is_public == True,  # noqa: E712
                ModelProvider.is_active == True,  # noqa: E712
            )
            .order_by(ModelProvider.id)
        )
        for provider in public:
            if capability in parse_capabilities(provider.capabilities):
                return provider

    raise LLMError(f"当前账号下没有支持 {capability} 能力的可用模型提供商，请先添加")


async def chat(
    messages: list[dict[str, Any]],
    provider: ModelProvider,
    *,
    response_format: dict[str, Any] | None = None,
    read_timeout: float | None = None,
) -> LLMResult:
    """
    发一轮对话请求，返回内容 + token + 耗时 + 实际尝试次数

    messages 直接透传 OpenAI 格式，所以纯文本、system 角色、多轮上下文、
    图文混合 content 都走这一个函数，不需要为视觉另开接口。
    """
    s = get_settings()
    body: dict[str, Any] = {
        "model": provider.model_name,
        "messages": messages,
        "max_tokens": provider.max_tokens,
        "temperature": provider.temperature,
    }
    if response_format is not None:
        body["response_format"] = response_format
    # 思考开关：默认不带这个字段——各家 OpenAI 兼容端点对未知参数的容忍度不一，
    # 只有显式配了 LLM_ENABLE_THINKING 才发出去
    if s.llm_enable_thinking is not None:
        body["enable_thinking"] = s.llm_enable_thinking

    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(
        read_timeout if read_timeout is not None else s.llm_timeout_read,
        connect=s.llm_timeout_connect,
    )

    started = time.perf_counter()
    max_attempts = s.llm_max_retries + 1
    last_error = "LLM 调用失败"

    async with _get_semaphore():
        for attempt in range(1, max_attempts + 1):
            try:
                data = await _post_once(url, headers, body, timeout)
                return _build_result(data, provider, started, attempt)
            except _Retryable as e:
                last_error = str(e)
                if attempt < max_attempts:
                    delay = _retry_delay(attempt, e.retry_after)
                    logger.debug("LLM 重试（第 %d 次，等 %.1fs）：%s", attempt, delay, last_error)
                    await asyncio.sleep(delay)
            except LLMError as e:
                # 不可恢复的错误也带上已尝试次数，否则计量会记成"一次就挂"
                raise LLMError(str(e), attempts=attempt) from e

    raise LLMError(f"{last_error}（共尝试 {max_attempts} 次）", attempts=max_attempts)


def _exc_text(exc: BaseException) -> str:
    """异常给用户的文字。

    httpx 的超时异常在 asyncio 看门下 str() 常为空串，直接拼进消息会得到
    "LLM 请求超时：（共尝试 3 次）" 这种冒号后面空着的病句，所以退到类名。
    """
    return str(exc).strip() or type(exc).__name__


async def _post_once(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeouts: httpx.Timeout,
) -> dict[str, Any]:
    """单次外呼。可恢复的失败抛 _Retryable，其余抛 LLMError"""
    try:
        resp = await get_client().post(url, headers=headers, json=body, timeout=timeouts)
    except httpx.TimeoutException as e:
        raise _Retryable(f"LLM 请求超时：{_exc_text(e)}") from e
    except httpx.RequestError as e:
        raise _Retryable(f"LLM 网络错误：{_exc_text(e)}") from e

    if resp.status_code != 200:
        # 响应体截断 500 字符，防止密钥等敏感信息整段进入错误消息
        msg = f"LLM 返回 {resp.status_code}：{resp.text[:500]}"
        if resp.status_code in _RETRYABLE_STATUS:
            hint = _parse_retry_after(resp.headers.get("Retry-After"))
            raise _Retryable(msg, retry_after=hint)
        raise LLMError(msg)

    try:
        return resp.json()
    except ValueError as e:
        raise LLMError(f"LLM 响应不是 JSON：{e}，body={resp.text[:500]}") from e


def _build_result(
    data: dict[str, Any],
    provider: ModelProvider,
    started: float,
    attempts: int,
) -> LLMResult:
    """把 OpenAI 响应拆成 LLMResult；usage 缺失按 0 计，不影响调用成功"""
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"LLM 响应结构异常：{e}，body={str(data)[:500]}") from e

    usage = data.get("usage") or {}
    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    return LLMResult(
        content=content or "",
        prompt_tokens=prompt,
        completion_tokens=completion,
        # 有的端点只给分项不给 total，自己补上，别让 SUM 出来是 0
        total_tokens=_as_int(usage.get("total_tokens")) or prompt + completion,
        latency_ms=int((time.perf_counter() - started) * 1000),
        model_name=data.get("model") or provider.model_name,
        finish_reason=choice.get("finish_reason"),
        attempts=attempts,
    )


def _as_int(value: Any) -> int:
    """计量字段容错：缺字段或类型异常都按 0 计，不因此判调用失败"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_retry_after(raw: str | None) -> float | None:
    """只认整数秒形式的 Retry-After；HTTP-date 形式不解析，退回指数退避"""
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 <= value <= _MAX_RETRY_AFTER else None


def _retry_delay(attempt: int, server_hint: float | None) -> float:
    """服务端给了等待时间就听它的，否则指数退避加抖动，避免并发请求同拍重试"""
    if server_hint is not None:
        return server_hint
    return random.uniform(0, min(2.0**attempt, _BACKOFF_CAP))


# ---------- 结构化输出 ----------


def _format_ladder(json_schema: dict[str, Any] | None) -> list[dict[str, Any] | None]:
    """按输出质量从高到低排候选；越靠后兼容性越好"""
    rungs: list[dict[str, Any] | None] = []
    if json_schema is not None:
        rungs.append({"type": "json_schema", "json_schema": json_schema})
    rungs.append({"type": "json_object"})
    rungs.append(None)
    return rungs


def _rejected_response_format(message: str) -> bool:
    """判断这次失败是不是端点不认 response_format，是就降到下一级再试"""
    lowered = message.lower()
    return any(kw in lowered for kw in ("response_format", "json_schema", "json_object"))


def _tier_name(response_format: dict[str, Any] | None) -> str:
    """把 response_format 折成档位名：评测台和日志只看这一串"""
    if response_format is None:
        return "plain"
    return str(response_format.get("type"))


async def chat_json(
    messages: list[dict[str, Any]],
    provider: ModelProvider,
    *,
    json_schema: dict[str, Any] | None = None,
    read_timeout: float | None = None,
) -> tuple[LLMResult, Any | None]:
    """
    要求模型输出 JSON，按 json_schema → json_object → 不带 response_format 三级降级

    国产 OpenAI 兼容端点对 response_format 的支持参差，硬绑一种会在换模型时整片失败。
    返回 (原始结果, 解析后的对象)；对象为 **None 表示调用成功但输出不是合法 JSON**，
    由调用方决定降级还是报错。用 json_object 档时提示词里必须含 "json" 字样，
    这是 OpenAI 的硬性要求，兼容端点通常也照抄。

    ⚠ 只在"端点明确拒绝 response_format"时降档。**输出解析失败就地返回、不再往下打**——
    降级方向是越来越松，同一份提示再打一次大概率还是烂 JSON，白烧一倍 token。

    成功那次的档位写进 `LLMResult.format_tier`，拒绝降档时打 INFO 日志：
    换端点后到底是模型变弱了还是严格档没被支持，必须能从这两处读出来。
    """
    last_error = "模型未返回可解析的 JSON"
    for response_format in _format_ladder(json_schema):
        try:
            result = await chat(
                messages, provider, response_format=response_format, read_timeout=read_timeout
            )
        except LLMError as e:
            if not _rejected_response_format(str(e)):
                raise
            last_error = str(e)
            logger.info(
                "端点不接受 response_format=%s，降到下一档重试",
                _tier_name(response_format),
            )
            continue

        tagged = replace(result, format_tier=_tier_name(response_format))
        try:
            return tagged, extract_json(tagged.content)
        except LLMError as e:
            # 模型不按格式出货是预期结果而不是异常。更松的一档只会更不像 JSON，
            # 所以把原始内容交回调用方自己兜，而不是再花钱打一次
            logger.debug("端点输出不可解析为 JSON，交调用方降级：%s", str(e)[:200])
            return tagged, None

    raise LLMError(last_error)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """
    从模型输出里抠出 JSON：先整体解析，再去 ``` 围栏，最后扫第一个括号平衡的片段

    模型很爱在 JSON 外面加解释文字或代码块，直接 json.loads 会白报错。
    """
    stripped = text.strip()
    if not stripped:
        raise LLMError("模型返回空内容，无法解析 JSON")

    unfenced = _FENCE_RE.sub("", stripped).strip()
    candidates = [stripped]
    if unfenced and unfenced != stripped:
        candidates.append(unfenced)
    balanced = _first_balanced(unfenced or stripped)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise LLMError(f"模型输出不是合法 JSON：{stripped[:200]}")


def _first_balanced(text: str) -> str | None:
    """扫描第一个括号平衡的片段，跳过字符串字面量里的括号。

    只跟踪自己这一种括号（起点是 { 就只数 {}），另一种直接忽略——
    JSON 里另一种括号本身也是平衡的，忽略不影响闭合判定。
    """
    opens = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not opens:
        return None
    start = min(opens)

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ---------- 计量 ----------


def record_llm_call(
    *,
    user_id: int,
    purpose: str,
    provider_id: int | None = None,
    provider_name: str | None = None,
    log_id: int | None = None,
    result: LLMResult | None = None,
    error: str | None = None,
    attempts: int = 1,
) -> None:
    """
    把一次外呼写进 llm_calls。同步函数，调用方用 run_in_threadpool 丢线程池

    只接原始值不接 ORM 对象：本函数会在线程池里跑，跨线程共用同一个 Session 不安全，
    而且 Session commit 后属性会过期。所以自带独立 session。

    失败时 attempts 由调用方从 LLMError 上取——不取的话计量里永远是 1，
    看不出"重试到耗尽才失败"和"一次就挂"的区别。

    计量是旁路：写失败只告警，绝不能因为记不上账就把已经成功的处理判成失败。
    """
    row = LlmCall(
        user_id=user_id,
        provider_id=provider_id,
        log_id=log_id,
        purpose=purpose,
        provider_name=provider_name,
        model_name=result.model_name if result else None,
        prompt_tokens=result.prompt_tokens if result else 0,
        completion_tokens=result.completion_tokens if result else 0,
        total_tokens=result.total_tokens if result else 0,
        latency_ms=result.latency_ms if result else 0,
        attempts=result.attempts if result else max(1, attempts),
        status="success" if result else "failed",
        error_message=error[:2000] if error else None,
    )

    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("LLM 计量写入失败 user_id=%s purpose=%s", user_id, purpose, exc_info=True)
    finally:
        db.close()
