"""
LLM 传输层单测 —— 覆盖 JSON 解析、能力路由、超时重试、并发闸、结构化输出降级

不连 Postgres、不出网：
- 纯函数直接调；
- HTTP 用假 client 替掉 services.llm.get_client；
- 提供商路由用内存 SQLite（只建这几张表，不碰 .env 里的业务库）。

异步不用 pytest-asyncio，测试函数里直接 asyncio.run()，省一个依赖。
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import services.llm as llm
from database import Base
from models.provider import ModelProvider
from schemas.provider import ProviderCreate
from services.secrets import encrypt_secret


# ---------- 测试脚手架 ----------
def _settings(**overrides):
    """替掉 get_settings，让测试不受本机 .env 影响"""
    base = {
        "llm_timeout_connect": 10.0,
        "llm_timeout_read": 120.0,
        "llm_max_retries": 2,
        "llm_max_concurrency": 4,
        "llm_trust_env_proxy": True,
        # None = 请求体不带 enable_thinking，与线上默认一致
        "llm_enable_thinking": None,
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
        # 库里存的是密文，测试构造的必须是真的行形态（services/secrets 不留明文后路）
        "api_key": encrypt_secret("sk-test-1234567890"),
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
def test_parse_capabilities_normalizes():
    """空值/纯空白都归一化为 {text}，大小写和多余空格不影响"""
    assert llm.parse_capabilities(None) == {"text"}
    assert llm.parse_capabilities("") == {"text"}
    assert llm.parse_capabilities("  ,, ") == {"text"}
    assert llm.parse_capabilities(" Text , VISION ") == {"text", "vision"}
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


def test_resolve_default_skips_inactive_and_cross_user(session):
    """默认选择跳过非活跃 provider，且绝不跨用户"""
    session.add(_provider(id=1, user_id=1, name="off", is_active=False))
    session.add(_provider(id=2, user_id=1, name="on"))
    session.add(_provider(id=3, user_id=2, name="others"))
    session.commit()

    assert llm.resolve_provider(session, 1, None, "text").name == "on"


def test_resolve_default_filters_by_capability(session):
    session.add(_provider(id=1, user_id=1, name="text-model", capabilities="text"))
    session.add(_provider(id=2, user_id=1, name="vl-model", capabilities="vision,text"))
    session.commit()

    assert llm.resolve_provider(session, 1, None, "vision").name == "vl-model"
    assert llm.resolve_provider(session, 1, None, "text").name == "text-model"


# ---------- chat：请求与结果 ----------
def test_chat_sends_correct_model_and_returns_content(reset_globals):
    """请求发到正确的模型，响应正确解析；端点回的 model 优先于配置"""
    client = _ScriptClient([_ok("结果", prompt_tokens=11, completion_tokens=7)])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(
        llm.chat(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], _provider()
        )
    )

    assert result.content == "结果"
    assert result.attempts == 1
    assert client.bodies[0]["model"] == "test-model"
    assert result.model_name == "answered-model"


def test_chat_computes_tokens_with_fallback(reset_globals):
    """端点给了分项就从分项求和，没给就归零——不能让 SUM 出来是假数"""
    client = _ScriptClient(
        [
            _ok(prompt_tokens=10, completion_tokens=5),
            _ok(),
        ]
    )
    reset_globals.setattr(llm, "get_client", lambda: client)

    r1 = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    assert (r1.prompt_tokens, r1.completion_tokens, r1.total_tokens) == (10, 5, 15)

    r2 = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    assert r2.total_tokens == 0


def test_chat_tolerates_null_and_empty_content(reset_globals):
    """content 为 null 当空串处理，choices 为空则报错"""
    client = _ScriptClient(
        [
            _Response(payload={"choices": [{"message": {"content": None}}]}),
            _Response(payload={"choices": []}),
        ]
    )
    reset_globals.setattr(llm, "get_client", lambda: client)

    r = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    assert r.content == ""

    with pytest.raises(llm.LLMError) as exc:
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    # 界面上只该看到人话；上游报文留在 detail 里给排障用
    assert "缺少必要字段" in str(exc.value)
    assert exc.value.code == "E-LLM-BADSTRUCT"
    assert "响应结构异常" in exc.value.detail


# ---------- 重试策略 ----------
def test_chat_does_not_retry_on_401(reset_globals, sleeps):
    """401 是密钥无效，重试也不会变对，一次就失败且不等待"""
    client = _ScriptClient([_Response(status_code=401, text="unauthorized")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError) as exc:
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    assert "密钥无效" in str(exc.value)
    assert exc.value.code == "E-LLM-AUTH"
    # 状态码与上游文字只进 detail/日志，不上界面
    assert "401" in exc.value.detail
    assert len(client.bodies) == 1
    assert sleeps == []


def test_chat_retries_then_reports_attempts(reset_globals, sleeps):
    """5xx/网络错误会重试，耗尽后错误消息带尝试次数；max_retries=0 也报 1 次"""
    # 5xx 重试后成功
    client = _ScriptClient([_Response(status_code=503, text="busy"), _ok("recovered")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    result = asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    assert result.content == "recovered"
    assert result.attempts == 2

    # 耗尽后错误消息带次数
    client2 = _ScriptClient([_Response(status_code=500, text="boom")] * 3)
    reset_globals.setattr(llm, "get_client", lambda: client2)

    with pytest.raises(llm.LLMError, match="共尝试"):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))


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


def test_chat_caps_inflight_requests(reset_globals):
    """并发闸确实限流：8 个请求同时发，峰值不超过上限"""
    reset_globals.setattr(
        llm, "get_settings", lambda: _settings(llm_max_concurrency=3, llm_max_retries=0)
    )

    state = {"active": 0, "peak": 0}

    class _SlowClient:
        async def post(self, url, headers=None, json=None, **ignored):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0)
            state["active"] -= 1
            return _ok("ok")

    client = _SlowClient()
    reset_globals.setattr(llm, "get_client", lambda: client)

    async def run():
        calls = [llm.chat([{"role": "user", "content": "x"}], _provider()) for _ in range(8)]
        return await asyncio.gather(*calls)

    results = asyncio.run(run())
    assert len(results) == 8
    assert state["peak"] <= 3


# ---------- extract_json ----------
def test_extract_json_handles_fences_and_prose():
    """围栏代码块和前后散文都能正确提取 JSON"""
    assert llm.extract_json('{"a": 1}') == {"a": 1}
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('好的，结果如下：\n{"a": 1}\n希望有帮助') == {"a": 1}


def test_extract_json_handles_strings_with_special_chars():
    """字符串内的花括号和转义引号不干扰解析"""
    parsed = llm.extract_json('{"text": "a } b { c", "n": 2}')
    assert parsed == {"text": "a } b { c", "n": 2}

    parsed2 = llm.extract_json('{"s": "he said \\"hi\\" loudly"}')
    assert parsed2["s"] == 'he said "hi" loudly'


def test_extract_json_top_level_array():
    assert llm.extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_rejects_empty():
    with pytest.raises(llm.LLMError, match="空内容"):
        llm.extract_json("   ")


def test_extract_json_rejects_invalid():
    with pytest.raises(llm.LLMError, match="没有按规定格式返回结果"):
        llm.extract_json("抱歉，我无法完成")
    with pytest.raises(llm.LLMError, match="没有按规定格式返回结果") as exc:
        llm.extract_json('{"a": 1')
    # 原文（模型输出片段）留在 detail，不显示给用户
    assert '{"a": 1' in exc.value.detail


# ---------- chat_json 降级 ----------
def test_chat_json_degrades_when_format_unsupported(reset_globals, sleeps):
    """端点拒绝 response_format 时逐级降级，而不是整条链路失败"""
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
    assert result.attempts == 1


def test_chat_json_propagates_auth_and_parse_errors(reset_globals, sleeps):
    """401 不被降级逻辑吃掉；烂 JSON 返回原始内容而非再打一次"""
    # 401 直接报错
    client = _ScriptClient([_Response(status_code=401, text="invalid api key")])
    reset_globals.setattr(llm, "get_client", lambda: client)
    with pytest.raises(llm.LLMError, match="密钥无效"):
        asyncio.run(llm.chat_json([{"role": "user", "content": "json"}], _provider()))

    # 烂 JSON 交回调用方
    client2 = _ScriptClient([_ok("抱歉，我无法输出 JSON")])
    reset_globals.setattr(llm, "get_client", lambda: client2)
    result, parsed = asyncio.run(llm.chat_json([{"role": "user", "content": "json"}], _provider()))
    assert parsed is None
    assert result.content == "抱歉，我无法输出 JSON"
    assert len(client2.bodies) == 1


# ---------- Retry-After 解析与退避 ----------
def test_retry_delay_respects_server_hint_and_cap():
    """服务端给的 Retry-After 优先用，否则按指数退避但不超上限"""
    assert llm._retry_delay(1, 2.5) == 2.5
    assert llm._retry_delay(99, None) <= llm._BACKOFF_CAP
    assert llm._retry_delay(1, None) > 0


def test_retry_delay_rejects_invalid_inputs():
    """HTTP 日期格式和超大值都当无效处理"""
    assert llm._parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert llm._parse_retry_after("3600") is None
    assert llm._parse_retry_after(None) is None


# ---------- 超时文案与思考开关 ----------
def test_timeout_message_names_the_exception_when_detail_is_empty(reset_globals):
    """
    httpx 在 asyncio 门下抛出的超时异常 str() 常为空串。

    早先直接 f"LLM 请求超时：{e}" 会产出"LLM 请求超时：（共尝试 3 次）"这种
    冒号后面空着的病句，用户看不出是超时还是别的。真机跑思考模型时踩到过。
    """
    client = _ScriptClient([httpx.ReadTimeout(""), httpx.ReadTimeout(""), httpx.ReadTimeout("")])
    reset_globals.setattr(llm, "get_client", lambda: client)

    with pytest.raises(llm.LLMError) as exc:
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))

    msg = str(exc.value)
    # 给人看的那句必须是完整句子，且空异常文本不会留下"冒号后空白"的病句
    assert "超时" in msg, msg
    assert "共尝试 3 次" in msg, msg
    assert "：（" not in msg, msg
    # 异常类名是排障线索，现在的落点是 detail（以及 llm_calls.error_message）
    assert "ReadTimeout" in exc.value.detail, exc.value.detail


def test_enable_thinking_absent_unless_configured(reset_globals):
    """默认不给端点塞未知参数；显式配置了才带 enable_thinking"""
    client = _ScriptClient([_ok("ok")])
    reset_globals.setattr(llm, "get_client", lambda: client)
    asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    assert "enable_thinking" not in client.bodies[0]

    client2 = _ScriptClient([_ok("ok")])
    reset_globals.setattr(llm, "get_client", lambda: client2)
    reset_globals.setattr(llm, "get_settings", lambda: _settings(llm_enable_thinking=False))
    asyncio.run(llm.chat([{"role": "user", "content": "x"}], _provider()))
    assert client2.bodies[0].get("enable_thinking") is False
