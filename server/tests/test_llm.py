"""
LLM 传输层单测 —— 覆盖 JSON 解析、能力路由、超时重试、并发闸、结构化输出降级

不连 MySQL、不出网：
- 纯函数直接调；
- HTTP 用假 client 替掉 services.llm.get_client；
- 提供商路由用内存 SQLite（只建这几张表，不碰 .env 里的 MySQL）。

异步不用 pytest-asyncio，测试函数里直接 asyncio.run()，省一个依赖。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import services.llm as llm
from database import Base
from models.provider import ModelProvider
from schemas.provider import ProviderCreate


# ---------- 测试脚手架 ----------
def _settings(**overrides):
    """替掉 get_settings，让测试不受本机 .env 影响"""
    base = {
        "llm_timeout_connect": 10.0,
        "llm_timeout_read": 120.0,
        "llm_max_retries": 2,
        "llm_max_concurrency": 4,
        "llm_trust_env_proxy": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _provider(**overrides) -> ModelProvider:
    """造一个不入库的 ModelProvider 实例，只用来读配置字段"""
    data = {
        "id": 1,
        "user_id": 1,
        "name": "test-provider",
        "base_url": "https://example.test/v1",
        "api_key": "sk-test-1234567890",
        "model_name": "test-model",
        "capabilities": "text",
        "max_tokens": 4096,
        "temperature": 0.7,
        "is_active": True,
    }
    data.update(overrides)
    return ModelProvider(**data)


class _Response:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(content="你好", **usage):
    return _Response(
        payload={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "model": "answered-model",
            "usage": usage,
        }
    )


class _ScriptClient:
    """按脚本依次返回响应或抛异常，并记录每次请求体"""

    def __init__(self, script):
        self.script = list(script)
        self.bodies = []

    async def post(self, url, headers=None, json=None, **ignored):
        self.bodies.append(json)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def reset_globals(monkeypatch):
    """每个测试重置进程级单例，并屏蔽真实退避等待"""
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_semaphore", None)
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    return monkeypatch


@pytest.fixture()
def sleeps(monkeypatch):
    """把 asyncio.sleep 换成记账版，退避等待不会真的拖慢测试"""
    recorded: list[float] = []

    async def fake_sleep(delay, *args, **kwargs):
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture()
def session():
    """内存 SQLite 会话，只建这几张表"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()


# ---------- 能力标记解析 ----------
def test_parse_capabilities_defaults_to_text():
    assert llm.parse_capabilities(None) == {"text"}
    assert llm.parse_capabilities("") == {"text"}
    assert llm.parse_capabilities("  ,, ") == {"text"}


def test_parse_capabilities_normalizes_case_and_spaces():
    assert llm.parse_capabilities(" Text , VISION ") == {"text", "vision"}


def test_parse_capabilities_multi_value():
    assert llm.parse_capabilities("text,vision,embedding") == {"text", "vision", "embedding"}


def test_schema_rejects_unknown_capability():
    with pytest.raises(ValueError, match="未知能力标记"):
        ProviderCreate(
            name="p",
            base_url="https://example.test/v1",
            api_key="sk-1",
            model_name="m",
            capabilities="textv",
        )


def test_schema_rejects_empty_capability():
    with pytest.raises(ValueError, match="至少需要一个"):
        ProviderCreate(
            name="p",
            base_url="https://example.test/v1",
            api_key="sk-1",
            model_name="m",
            capabilities=" , ",
        )


def test_schema_reorders_capabilities_to_canonical_form():
    body = ProviderCreate(
        name="p",
        base_url="https://example.test/v1",
        api_key="sk-1",
        model_name="m",
        capabilities="vision,text",
    )
    # 顺序归一化：同一集合永远序列化成同一个串
    assert body.capabilities == "text,vision"


def test_schema_defaults_capabilities_to_text():
    body = ProviderCreate(
        name="p", base_url="https://example.test/v1", api_key="sk-1", model_name="m"
    )
    assert body.capabilities == "text"


# ---------- resolve_provider：隔离与能力路由 ----------
def test_resolve_returns_explicit_provider(session):
    a = _provider(user_id=1, name="a", capabilities="text")
    b = _provider(id=2, user_id=1, name="b", capabilities="text")
    session.add_all([a, b])
    session.commit()

    assert llm.resolve_provider(session, 1, b.id, "text").name == "b"


def test_resolve_rejects_other_users_provider(session):
    """越权指定别人的提供商要报错，而且不能把他人的名称带进错误消息"""
    session.add(_provider(user_id=2, name="victim"))
    session.commit()

    with pytest.raises(llm.LLMError, match="已被删除或不可用"):
        llm.resolve_provider(session, 1, 1, "text")


def test_resolve_rejects_missing_capability(session):
    session.add(_provider(user_id=1, name="text-only", capabilities="text"))
    session.commit()

    with pytest.raises(llm.LLMError, match="不支持 vision 能力"):
        llm.resolve_provider(session, 1, 1, "vision")


def test_resolve_default_picks_lowest_active_id(session):
    low = _provider(id=5, user_id=1, name="low", capabilities="text")
    high = _provider(id=9, user_id=1, name="high", capabilities="text")
    session.add_all([high, low])
    session.commit()

    assert llm.resolve_provider(session, 1, None, "text").name == "low"


def test_resolve_default_skips_inactive(session):
    session.add(_provider(id=1, user_id=1, name="off", is_active=False))
    session.add(_provider(id=2, user_id=1, name="on"))
    session.commit()

    assert llm.resolve_provider(session, 1, None, "text").name == "on"


def test_resolve_default_never_crosses_users(session):
    session.add(_provider(id=1, user_id=2, name="mine-only"))
    session.commit()

    with pytest.raises(llm.LLMError, match="没有支持 text 能力"):
        llm.resolve_provider(session, 1, None, "text")


def test_resolve_default_filters_by_capability(session):
    session.add(_provider(id=1, user_id=1, name="text-model", capabilities="text"))
    session.add(_provider(id=2, user_id=1, name="vl-model", capabilities="vision,text"))
    session.commit()

    assert llm.resolve_provider(session, 1, None, "vision").name == "vl-model"
    assert llm.resolve_provider(session, 1, None, "text").name == "text-model"


# ---------- chat：请求体与结果 ----------
def test_chat_sends_openai_body(reset_globals):
    client = _ScriptClient([_ok("结果", prompt_tokens=11, completion_tokens=7)])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(
        llm.chat(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], _provider()
        )
    )

    assert result.content == "结果"
    assert result.attempts == 1
    assert client.bodies[0]["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert client.bodies[0]["model"] == "test-model"


def test_chat_computes_total_tokens_from_parts(reset_globals):
    client = _ScriptClient([_ok(prompt_tokens=10, completion_tokens=5)])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    # 端点没给 total_tokens 时自己求和，别让 SUM 出来是 0
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (10, 5, 15)


def test_chat_zeroes_missing_usage(reset_globals):
    client = _ScriptClient([_ok()])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert result.total_tokens == 0


def test_chat_tolerates_null_content(reset_globals):
    client = _ScriptClient([_Response(payload={"choices": [{"message": {"content": None}}]})])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert result.content == ""


def test_chat_raises_on_empty_choices(reset_globals):
    client = _ScriptClient([_Response(payload={"choices": []})])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError, match="响应结构异常"):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))


def test_chat_uses_model_from_response(reset_globals):
    client = _ScriptClient([_ok()])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    # 端点回的 model 优先于配置的别名，计量要记真正跑了哪个
    assert result.model_name == "answered-model"


# ---------- 重试策略 ----------
def test_chat_does_not_retry_on_401(reset_globals, sleeps):
    """401 是密钥无效，重试也不会变对，一次就失败"""
    client = _ScriptClient([_Response(status_code=401, text="unauthorized")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError, match="401"):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert len(client.bodies) == 1
    assert sleeps == []


def test_chat_retries_5xx_then_succeeds(reset_globals, sleeps):
    client = _ScriptClient([_Response(status_code=503, text="busy"), _ok(" recovered")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert result.content == " recovered"
    assert result.attempts == 2
    assert len(sleeps) == 1


def test_chat_honors_retry_after(reset_globals, sleeps):
    client = _ScriptClient(
        [_Response(status_code=429, text="slow down", headers={"Retry-After": "3"}), _ok("ok")]
    )
    reset_globals.setattr(llm, "get_client", lambda: client)

    asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    # 服务端明确给了等待秒数，就别用随机退避
    assert sleeps == [3.0]


def test_chat_retries_network_error(reset_globals, sleeps):
    import httpx

    client = _ScriptClient(
        [
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
            _ok("ok"),
        ]
    )
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert result.attempts == 3


def test_chat_exhausts_retries_with_attempts_in_message(reset_globals, sleeps):
    reset_globals.setattr(llm, "get_settings", lambda: _settings(llm_max_retries=1))
    client = _ScriptClient(
        [_Response(status_code=500, text="boom"), _Response(status_code=500, text="boom")]
    )
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError, match="共尝试 2 次"):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))


def test_chat_zero_retries_means_single_attempt(reset_globals, sleeps):
    reset_globals.setattr(llm, "get_settings", lambda: _settings(llm_max_retries=0))
    client = _ScriptClient([_Response(status_code=502, text="bad gateway")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError, match="共尝试 1 次"):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert len(client.bodies) == 1


def test_llm_error_carries_attempts_when_retries_exhausted(reset_globals, sleeps):
    """失败也必须带上重试次数，否则计量里永远是 attempts=1，看不出差别"""
    client = _ScriptClient([_Response(status_code=503, text="busy")] * 3)
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError) as exc_info:
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert exc_info.value.attempts == 3


def test_llm_error_carries_attempt_count_for_permanent_4xx(reset_globals, sleeps):
    client = _ScriptClient([_Response(status_code=401, text="denied")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError) as exc_info:
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert exc_info.value.attempts == 1


def test_llm_error_defaults_attempts_to_one():
    assert llm.LLMError("boom").attempts == 1


# ---------- 并发闸 ----------
def test_chat_caps_inflight_requests(reset_globals):
    reset_globals.setattr(
        llm, "get_settings", lambda: _settings(llm_max_concurrency=3, llm_max_retries=0)
    )

    state = {"active": 0, "peak": 0}

    class _SlowClient:
        async def post(self, url, headers=None, json=None, **ignored):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0)  # 让出控制权，逼出真实并发
            state["active"] -= 1
            return _ok("ok")

    client = _SlowClient()
    reset_globals.setattr(llm, "get_client", lambda: client)

    async def run():
        calls = [llm.chat([{"role": "user", "content": "x"}], _provider()) for _ in range(8)]
        return await asyncio.gather(*calls)

    results = asyncio.run(run())

    assert len(results) == 8
    # 下限证明确实并发了，上限证明闸生效
    assert 2 <= state["peak"] <= 3


# ---------- extract_json ----------
def test_extract_json_plain():
    assert llm.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_prose_around():
    assert llm.extract_json('好的，结果如下：\n{"a": 1}\n希望有帮助') == {"a": 1}


def test_extract_json_braces_inside_string_are_ignored():
    parsed = llm.extract_json('{"text": "a } b { c", "n": 2}')
    assert parsed == {"text": "a } b { c", "n": 2}


def test_extract_json_nested_other_brackets_survive():
    raw = '{"items": [1, 2], "meta": {"k": "v"}}'
    assert llm.extract_json(raw) == json.loads(raw)


def test_extract_json_handles_escaped_quote():
    parsed = llm.extract_json('{"s": "he said \\"hi\\" loudly"}')
    assert parsed["s"] == 'he said "hi" loudly'


def test_extract_json_top_level_array():
    assert llm.extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_rejects_empty():
    with pytest.raises(llm.LLMError, match="空内容"):
        llm.extract_json("   ")


def test_extract_json_rejects_no_json():
    with pytest.raises(llm.LLMError, match="不是合法 JSON"):
        llm.extract_json("抱歉，我无法完成")


def test_extract_json_rejects_unbalanced():
    with pytest.raises(llm.LLMError, match="不是合法 JSON"):
        llm.extract_json('{"a": 1')


# ---------- chat_json 降级 ----------
def test_chat_json_degrades_when_schema_rejected(reset_globals, sleeps):
    """端点不认 json_schema 时降到 json_object，而不是整条链路失败"""
    rejected = _Response(status_code=400, text='{"error":"response_format is not supported"}')
    client = _ScriptClient([rejected, _ok('{"score": 90}')])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result, parsed = asyncio.run(
        llm.chat_json(
            [{"role": "user", "content": "给出 json"}],
            _provider(),
            json_schema={"name": "grade", "schema": {"type": "object"}},
        )
    )

    assert parsed == {"score": 90}
    # 降级是另起一次调用，不是同一次调用的重试，attempts 仍是 1
    assert result.attempts == 1
    assert client.bodies[0]["response_format"]["type"] == "json_schema"
    assert client.bodies[1]["response_format"]["type"] == "json_object"


def test_chat_json_drops_response_format_entirely_when_unsupported(reset_globals, sleeps):
    """不传 json_schema 时梯子只有两档：json_object → 完全不带 response_format"""
    rejected = _Response(status_code=400, text="bad response_format")
    client = _ScriptClient([rejected, _ok('{"ok": true}')])
    reset_globals.setattr(llm, "get_client", lambda: client)

    _, parsed = asyncio.run(llm.chat_json([{"role": "user", "content": "json"}], _provider()))

    assert parsed == {"ok": True}
    assert client.bodies[0]["response_format"]["type"] == "json_object"
    assert "response_format" not in client.bodies[1]
    assert len(client.bodies) == 2


def test_chat_json_propagates_auth_error_without_degrading(reset_globals, sleeps):
    """401 跟 response_format 无关，不该被降级逻辑吃掉重试一遍"""
    client = _ScriptClient([_Response(status_code=401, text="invalid api key")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError, match="401"):
        asyncio.run(llm.chat_json([{"role": "user", "content": "json"}], _provider()))

    assert len(client.bodies) == 1


def test_chat_json_returns_none_inst_of_raising_on_unparseable_output(reset_globals, sleeps):
    """烂 JSON 是预期结果不是异常：把原始内容交回调用方，且绝不再打第二次"""
    client = _ScriptClient([_ok("抱歉，我无法输出 JSON")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result, parsed = asyncio.run(llm.chat_json([{"role": "user", "content": "json"}], _provider()))

    assert parsed is None
    assert result.content == "抱歉，我无法输出 JSON"
    assert len(client.bodies) == 1


# ---------- Retry-After 解析与退避 ----------
def test_parse_retry_after_accepts_plain_seconds():
    assert llm._parse_retry_after("5") == 5.0
    assert llm._parse_retry_after("0") == 0.0


def test_parse_retry_after_rejects_http_date():
    assert llm._parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_parse_retry_after_rejects_oversized_hint():
    assert llm._parse_retry_after("3600") is None


def test_parse_retry_after_handles_missing_header():
    assert llm._parse_retry_after(None) is None


def test_retry_delay_capped_by_backoff_ceiling():
    assert llm._retry_delay(99, None) <= llm._BACKOFF_CAP


def test_retry_delay_prefers_server_hint():
    assert llm._retry_delay(1, 2.5) == 2.5
