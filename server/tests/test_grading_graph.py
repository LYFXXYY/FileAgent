"""
分块批改图单测 —— 扇出、全局偏移、拼回、降级、按块续跑

不连 Postgres、不出网：HTTP 用假 client（跟 test_llm.py 同一套路），检查点和 store
都走异步 SQLite，落在 tmp_path。生产入口 `grade_long_document` 不在这里直接调，
改成调用内部可注入函数 `_grade_with_backend`，绕开 Postgres 依赖。

【为什么假 client 要按请求内容路由】
`Send` 扇出的分支并发跑，谁先打到端点不确定。按顺序吐响应的脚本 client 会把第 1 块
的答复发给第 3 块，测出来的偏移和拼回全是碰运气——那种测试只会让人误以为图是对的。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore
from test_llm import _provider, _Response

import services.llm as llm
from graphs import grading_graph
from graphs.grading_graph import (
    _grade_with_backend,
    _read_progress_with,
    derive_pg_dsn,
)

# 三段各 19–20 字的文档，预算 30 字 → 稳定切成 3 块（每块正好一段）
DOC = "\n".join(
    [
        "第一段里有一处错别字：他高兴的跳了起来。",
        "第二段也有问题：认真的记录数据很重要。",
        "第三段最后写：这道题做的很顺利。",
    ]
)

# (块内特征串, 错处原文, 正确写法, 该块修订文)
FIXES = [
    ("他高兴的跳了起来", "他高兴的跳了起来", "他高兴地跳了起来", "第一段改后。"),
    ("认真的记录数据", "认真的记录数据", "认真地记录数据", "第二段改后。"),
    ("这道题做的很顺利", "这道题做的很顺利", "这道题做得很顺利", "第三段改后。"),
]
INSTRUCTION = "请批改：\n{{content}}"
EXPECTED_REVISED = "第一段改后。\n第二段改后。\n第三段改后。"


def _settings(**overrides):
    base = {
        "llm_timeout_connect": 10.0,
        "llm_timeout_read": 30.0,
        "llm_max_retries": 0,
        "llm_max_concurrency": 4,
        "llm_trust_env_proxy": True,
        "llm_enable_thinking": None,
        "longdoc_chunk_chars": 30,
        "longdoc_enabled": True,
        "database_url": "postgresql+psycopg2://x:y@localhost:5434/file_agent",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ok(content: str) -> _Response:
    return _Response(
        payload={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "model": "answered-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
    )


def _grade_payload(mark: str, fragment: str, fixed: str, revised: str) -> str:
    return json.dumps(
        {
            "revised_text": revised,
            "issues": [
                {
                    "type": "typo",
                    "original": fragment,
                    "suggestion": fixed,
                    "reason": "的地得误用",
                    "confidence": 0.9,
                }
            ],
        },
        ensure_ascii=False,
    )


class _RouteClient:
    """
    按请求里的块特征串决定响应，并记录每块被调用了几次

    free_text / ghost / die 都是块的序号（0 起），用来造降级、错摘原文、外呼失败。
    """

    def __init__(self, *, die=(), free_text=(), ghost=()):
        self.die, self.free_text, self.ghost = set(die), set(free_text), set(ghost)
        self.calls = []

    def _which(self, body: dict) -> int:
        content = body["messages"][-1]["content"]
        for i, (mark, *_rest) in enumerate(FIXES):
            if mark in content:
                return i
        raise AssertionError(f"请求里没有已知块：{content[:60]}")

    async def post(self, url, headers=None, json=None, **ignored):
        idx = self._which(json)
        self.calls.append(idx)
        if idx in self.die:
            raise llm.LLMError("端点连不上（共尝试 1 次）")
        mark, fragment, fixed, revised = FIXES[idx]
        if idx in self.free_text:
            return _ok("这段不是 JSON，就是一段自由文本。")
        if idx in self.ghost:
            return _ok(_grade_payload(mark, "根本不存在的句子", fixed, revised))
        return _ok(_grade_payload(mark, fragment, fixed, revised))


def _sqlite_paths(tmp_path):
    """两个 sqlite 文件路径：图状态 + 按块缓存。同文件不同连接会撞库锁"""
    return str(tmp_path / "ckpt.sqlite3"), str(tmp_path / "store.sqlite3")


def _run(tmp_path, client, *, thread_id="log-1", instruction=INSTRUCTION, spec_lookup=None):
    """跑一轮图。每个调用都用全新的 sqlite 文件路径（tmp_path 是函数级 fixture）"""
    llm._client = None
    llm._semaphore = None
    llm.get_settings = lambda: _settings()
    grading_graph.get_settings = lambda: _settings()
    llm.get_client = lambda: client

    ckpt_path, store_path = _sqlite_paths(tmp_path)

    async def go():
        async with AsyncSqliteStore.from_conn_string(store_path) as store:
            await store.setup()
            async with AsyncSqliteSaver.from_conn_string(ckpt_path) as saver:
                await saver.setup()
                return await _grade_with_backend(
                    DOC,
                    _provider(),
                    instruction,
                    thread_id=thread_id,
                    checkpointer=saver,
                    store=store,
                    spec_lookup=spec_lookup,
                )

    return asyncio.run(go())


def _reuse(tmp_path, client, *, thread_id="log-1", instruction=INSTRUCTION, spec_lookup=None):
    """保留同一对 sqlite 文件，模拟续跑场景"""
    llm._client = None
    llm._semaphore = None
    llm.get_settings = lambda: _settings()
    grading_graph.get_settings = lambda: _settings()
    llm.get_client = lambda: client

    ckpt_path, store_path = _sqlite_paths(tmp_path)

    async def go():
        async with AsyncSqliteStore.from_conn_string(store_path) as store:
            await store.setup()
            async with AsyncSqliteSaver.from_conn_string(ckpt_path) as saver:
                await saver.setup()
                return await _grade_with_backend(
                    DOC,
                    _provider(),
                    instruction,
                    thread_id=thread_id,
                    checkpointer=saver,
                    store=store,
                    spec_lookup=spec_lookup,
                )

    return asyncio.run(go())


@pytest.fixture()
def env(tmp_path):
    """
    返回带 tmp_path 的句柄：同一次测试内多次 _run/_reuse 共用同一对 sqlite 文件

    `ckpt_path` / `store_path` 用函数级 fixture 名字保留，测试里读它们给 _read_progress_with。
    """
    ckpt_path, store_path = _sqlite_paths(tmp_path)
    return SimpleNamespace(tmp_path=tmp_path, ckpt=ckpt_path, store=store_path)


def _fresh_client_run(env, client, **kw):
    """每次都从干净文件开始（避免上一用例污染）"""
    return _run(env.tmp_path, client, **kw)


def _continue_run(env, client, **kw):
    """保留同一对文件，模拟续跑场景"""
    return _reuse(env.tmp_path, client, **kw)


def test_document_is_split_and_each_chunk_called_once(env):
    client = _RouteClient()
    result = _fresh_client_run(env, client)

    assert result.chunk_count == 3
    assert sorted(client.calls) == [0, 1, 2], "每块恰好一次外呼，不许漏块也不许多打"


def test_issue_offsets_are_global(env):
    """
    块内偏移必须换成整篇偏移——前端按 original_content 全文高亮，
    偏移错一位批注就指到别的句子上，而且没有任何信号
    """
    result = _fresh_client_run(env, _RouteClient())

    assert len(result.issues) == 3
    for issue in result.issues:
        assert issue["start"] is not None
        assert DOC[issue["start"] : issue["end"]] == issue["original"]


def test_revised_text_is_reassembled_in_document_order(env):
    result = _fresh_client_run(env, _RouteClient())

    assert result.revised_text == EXPECTED_REVISED
    assert result.degraded is False
    assert result.cached_chunks == 0


def test_unlocatable_fragment_stays_none(env):
    """模型摘了原文里不存在的片段时不许猜位置，留 None 让前端显示未定位"""
    result = _fresh_client_run(env, _RouteClient(ghost=(0,)))

    assert result.issues[0]["start"] is None, "第 1 块的片段不在第 1 块里，不该有偏移"
    for issue in result.issues[1:]:
        assert DOC[issue["start"] : issue["end"]] == issue["original"], "其余块照常全局定位"


def test_one_bad_chunk_degrades_without_failing_the_document(env):
    """某一块吐自由文本时只降级那一段，其余块的结构化结果必须保住"""
    result = _fresh_client_run(env, _RouteClient(free_text=(1,)))

    assert result.degraded is True
    assert len(result.issues) == 2, "坏块不该连累其它块的意见"
    assert "这段不是 JSON" in result.revised_text
    assert result.revised_text.count("\n") == 2, "降级块也要占住原来的段落位置"


def test_retry_only_bills_the_failed_chunk(env):
    """
    第 2 块外呼失败后重跑：成功过的两块不该再花一次钱

    检查点只按 super-step 落盘，扇出里一步失败会把整步写入一起作废，所以这件事
    靠的是按块 store，不是靠 checkpoint——两样都得留着。
    """
    first = _RouteClient(die=(1,))
    with pytest.raises(llm.LLMError):
        _continue_run(env, first)
    assert sorted(first.calls) == [0, 1, 2], "三块都试过，其中两块成功"

    retry = _RouteClient()
    result = _continue_run(env, retry)

    assert retry.calls == [1], f"续跑只该重打失败的那块，实际打了 {retry.calls}"
    assert result.cached_chunks == 2
    assert result.revised_text == EXPECTED_REVISED


def test_progress_reads_total_and_done_from_checkpoint(env):
    """跑完之后进度必须是 N/N，前端靠它区分是卡住了还是快好了"""
    _continue_run(env, _RouteClient())

    async def read():
        async with AsyncSqliteSaver.from_conn_string(env.ckpt) as saver:
            await saver.setup()
            return await _read_progress_with(saver, "log-1")

    progress = asyncio.run(read())
    assert progress is not None
    assert (progress.total, progress.done) == (3, 3)


def test_progress_is_none_for_unknown_thread(env):
    """没跑过的 thread 返回 None，不是 0/0：没有依据的数字比没有数字更坏"""

    async def read():
        async with AsyncSqliteSaver.from_conn_string(env.ckpt) as saver:
            await saver.setup()
            return await _read_progress_with(saver, "log-does-not-exist")

    assert asyncio.run(read()) is None


def test_progress_does_not_pretend_after_a_failed_fanout(env):
    """
    扇出里一块失败时，整步检查点写入一起作废，所以 done 不该显示"跑了一半"

    这条断言写的是**行为现状**而不是愿望：钱确实不会重复烧（按块缓存在 store 里），
    但进度数字会跳，别在前端把它抹平成一个更好看的版本。
    """
    with pytest.raises(llm.LLMError):
        _continue_run(env, _RouteClient(die=(1,)))

    async def read():
        async with AsyncSqliteSaver.from_conn_string(env.ckpt) as saver:
            await saver.setup()
            return await _read_progress_with(saver, "log-1")

    progress = asyncio.run(read())
    assert progress is None or progress.done == 0


def test_cache_is_invalidated_when_instruction_changes(env):
    """换了提示词还在吃旧结果，看起来"变好了"其实是缓存，比 bug 难查"""
    _continue_run(env, _RouteClient())

    second = _RouteClient()
    result = _continue_run(env, second, instruction=INSTRUCTION + "\n顺便统计字数。")

    assert sorted(second.calls) == [0, 1, 2], "指令变了必须重打"
    assert result.cached_chunks == 0


def test_derive_pg_dsn_strips_sqlalchemy_driver_prefix():
    """SQLAlchemy 用 `postgresql+psycopg2://`，psycopg3 要裸 `postgresql://`"""
    assert derive_pg_dsn("postgresql+psycopg2://u:p@h:5434/d") == "postgresql://u:p@h:5434/d"
    assert derive_pg_dsn("postgresql+psycopg://u:p@h/d") == "postgresql://u:p@h/d"
    # 已经是裸串就原样返回，不再次剥前缀
    assert derive_pg_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"


# ---------- 按块注入规范条款（阶段 6 A6） ----------
class _RecordingClient(_RouteClient):
    """记下每次请求的 messages，用来证明条款是按块进的那条消息"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.messages = []

    async def post(self, url, headers=None, json=None, **ignored):
        self.messages.append([dict(m) for m in json["messages"]])
        return await super().post(url, headers=headers, json=json, **ignored)


def _make_lookup(seen):
    async def lookup(chunk_text: str):
        seen.append(chunk_text)
        clause = f"{len(seen)}.1.1 按块条款"
        return (
            f"【{clause}】判定标准见条文",
            [{"clause": clause, "score": 0.5, "via": "lexical", "doc_id": 1, "seq": len(seen)}],
            None,
        )

    return lookup


def test_spec_lookup_runs_once_per_chunk_with_that_chunk_text(tmp_path):
    """
    长文档必须按块查询：整篇查一次等于只按开头几段检索（bge 还会静默截断长输入）。

    断言两件事：每块各查一次、查询用的文本就是那一块的文本。
    """
    seen: list[str] = []
    client = _RecordingClient()
    _run(tmp_path, client, spec_lookup=_make_lookup(seen))

    assert len(seen) == 3
    for chunk in seen:
        assert chunk and chunk in DOC
    assert len({c for c in seen}) == 3  # 三块文本互不相同


def test_chunk_context_lands_in_that_chunk_user_message(tmp_path):
    """条款进对应块的那条 user 消息，不进 system（契约不被污染）"""
    seen: list[str] = []
    client = _RecordingClient()
    _run(tmp_path, client, spec_lookup=_make_lookup(seen))

    assert len(client.messages) == 3
    for idx, msgs in enumerate(client.messages):
        user = next(m["content"] for m in msgs if m["role"] == "user")
        system = next(m["content"] for m in msgs if m["role"] == "system")
        assert f"{idx + 1}.1.1 按块条款" in user
        assert "按块条款" not in system


def test_refs_are_union_across_chunks(tmp_path):
    """三块各召回一条 → 并集三条，且都还能溯源"""
    seen: list[str] = []
    result = _run(tmp_path, _RecordingClient(), spec_lookup=_make_lookup(seen))

    clauses = [r["clause"] for r in result.refs]
    assert len(clauses) == 3
    assert len(set(clauses)) == 3  # 三块各一条，没有互相覆盖
    assert result.spec_note is None


def test_no_spec_lookup_keeps_prompts_clean(tmp_path):
    """不传 lookup 时不注入任何条款段、refs 为空——长文档路径与阶段 6 之前一致"""
    client = _RecordingClient()
    result = _run(tmp_path, client)

    assert result.refs == ()
    assert result.spec_note is None
    for msgs in client.messages:
        user = next(m["content"] for m in msgs if m["role"] == "user")
        assert "规范库" not in user and "判定标准" not in user


def test_cached_chunk_replays_refs_without_re_querying(tmp_path):
    """
    命中按块缓存的块不再查询，但引用必须照样报出来。

    否则"重跑一次引用就消失了"——用户会以为规范库变空了，其实是缓存把引用吞了。
    """
    first: list[str] = []
    _run(tmp_path, _RecordingClient(), spec_lookup=_make_lookup(first))
    assert len(first) == 3

    second: list[str] = []
    result = _reuse(tmp_path, _RecordingClient(), spec_lookup=_make_lookup(second))

    assert second == []  # 三块全命中缓存，一次都没查
    assert len(result.refs) == 3
