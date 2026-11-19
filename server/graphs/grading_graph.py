"""
长文档分块批改图（阶段 3，2026-11-16 起检查点 / 缓存迁到 Postgres）—— LangGraph 扇出扇入 + 续跑

【图形状】

    START → plan → (Send 扇出) → grade_chunk × N → merge → END

`plan` 只算块边界不花钱；`grade_chunk` 是唯一的外呼节点；`merge` 把逐块结果按原位置
拼回一篇，并把每块的局部偏移换成全局偏移（前端按整篇原文高亮，见 §3.7）。

【省钱靠两样，缺一不可，别只留一个】
- **检查点（AsyncPostgresSaver）**：按 super-step 落盘到 `checkpoints` 表。它管的是"进程在两步
  之间没了，下次从这一步之后接着跑"。**它不管块级失败**——同一个扇出步里有一块抛异常，
  整步的写入一起作废，别的块的结果读不回来。
- **按块幂等存储（AsyncPostgresStore）**：每块成功外呼后立刻把结果写进 store 表，节点开头先查。
  键是 `(thread_id, chunk_index)`，命中还要求**块文本、指令摘要、模型名**三者一致，
  换提示词或换模型都不会吃到旧结果。这才是"第 99 块挂了别把前 98 块再烧一遍"的根据。

命中的块**不再写 llm_calls**：它这轮没发生外呼，记账进去就是重复计成本。

【为什么用 Postgres 而不是 SQLite（2026-11-16 阶段 4 改）】
- SQLite 时代两个连接（检查点 + 缓存）写同目录两个 .sqlite3 文件，靠分文件避开锁；
  Postgres 一库到底，连接池由 psycopg 自己管，并发场景比 SQLite 稳。
- 业务库（users/tasks/operation_logs）已经在 Postgres，检查点和缓存同库便于备份与跨进程共享。
- 阶段 6 pgvector 上 embedding 时也是这张库，不再多引入一个存储后端。

【这里刻意没有的东西】
- **图级 RetryPolicy**：`chat()` 已经按白名单重试过一轮，再套一层就是把同一个提示
  打两次、双倍烧钱。分层重试只留一层。
- **interrupt 人工确认**：确认"分块方案对不对"要先有 UI，图内挂起等确认是下一步的事。
- **节点里查库**：provider 由调用方解析后用闭包传进来。节点只碰模型，
  才能像 `services/llm.py` 那样用假 client 单测（§3.6 的边界不破）。

【依赖注入边界】
`grade_long_document` 是**生产入口**，自己从 `settings.database_url` 派生 psycopg3 DSN 并构造
AsyncPostgres 两件套；`_grade_with_backend` 是**测试入口**，调用方负责提供 checkpointer/store。
单测走 SQLite（`AsyncSqliteSaver` + `AsyncSqliteStore`）落在 tmp_path，不连 Postgres、不出网，
保持 §3.6 测试边界。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypedDict

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Send
from psycopg.rows import dict_row

from config import get_settings
from models.provider import ModelProvider
from schemas.grading import GRADING_JSON_SCHEMA
from services.chunking import Chunk, reassemble, split_document
from services.grading import build_messages, parse_grading_output
from services.llm import LLMError, LLMResult, chat_json

logger = logging.getLogger("fileagent.graph")

# 扇出上限：一次塞几十个分支进图，排到信号闸外面只会白白占内存
_MAX_INFLIGHT_HINT = 8

# psycopg 默认 connect_timeout=None，Windows 上 TCP 握手会卡到内核层超时（数十秒）才报错，
# 用户体验等同于"处理挂了"。5 秒足够本地 Docker 与同机房 Postgres，跨地域要放宽。
_CONNECT_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def _open_pg_conn(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """
    自己握 psycopg.AsyncConnection 的上下文，强制带 connect_timeout

    AsyncPostgresSaver/Store.from_conn_string 没透传 timeout，psycopg 默认 None，
    踩 Windows 的坑——所以这里手工建连接再喂给 saver/store 的 __init__。
    """
    async with await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
    ) as conn:
        yield conn


class _AsyncStore(Protocol):
    """aget/aput 这一对接口 Sqlite / Postgres 异步 store 都满足，单测才能不连库"""

    async def aget(self, namespace: tuple[str, ...], key: str) -> Any: ...

    async def aput(self, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None: ...


class _AsyncCheckpointer(Protocol):
    """aget_tuple 是 LangGraph 检查点的最小读接口"""

    async def aget_tuple(self, config: dict[str, Any]) -> Any: ...


class ChunkPayload(TypedDict, total=False):
    """`Send` 给单块分支的状态：只有块本身，没有整篇文档"""

    index: int
    start: int
    end: int
    text: str


class ChunkResult(TypedDict, total=False):
    """一块批完的产出。全是标量与 list/dict，要能进 msgpack 检查点"""

    index: int
    revised: str
    issues: list[dict[str, Any]]
    degraded: bool
    tokens: int
    completion_tokens: int
    prompt_tokens: int
    latency_ms: int
    attempts: int
    tier: str | None
    finish_reason: str | None
    # 本轮有没有真的外呼过。命中的块不写 llm_calls，否则重复计成本
    cached: bool
    # 这一块进过 prompt 的规范条款（阶段 6）。跟着结果一起进按块缓存：
    # 命中缓存时不重新编码，但引用必须照样能报出来，否则重跑一次"引用就消失了"
    refs: list[dict[str, Any]]
    spec_note: str | None


# 按块查询规范条款的回调：给一块文本，返回（条款上下文, 引用, 降级说明）。
# 由调用方闭包传进图里（同 provider/store），节点自己不碰 DB
SpecLookup = Callable[[str], Awaitable[tuple[str, list[dict[str, Any]], str | None]]]


def _upsert_results(old: list[ChunkResult], new: list[ChunkResult]) -> list[ChunkResult]:
    """
    按块序号覆盖，而不是追加

    用 `operator.add` 的话，同一 thread 的历次运行会堆在一起：用户点第二次"处理"时
    3 块变 6 段，`reassemble` 立刻报"块数与段数不一致"。追加语义是给聊天式线程用的，
    重跑一条日志要的是"这次的结果就是最终结果"。
    """
    by_index = {r["index"]: r for r in (*old, *new)}
    return [by_index[i] for i in sorted(by_index)]


class GradingState(TypedDict, total=False):
    source: str
    plan: list[ChunkPayload]
    # 扇出的分支各自上报，靠 reducer 按块序号合并
    results: Annotated[list[ChunkResult], _upsert_results]
    revised: str
    issues: list[dict[str, Any]]
    degraded: bool


@dataclass(frozen=True)
class ChunkCall:
    """单块的外呼计量数据，交回调用方写 llm_calls（图不碰库）"""

    index: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    attempts: int
    tier: str | None
    finish_reason: str | None
    degraded: bool
    cached: bool

    def as_llm_result(self, model_name: str) -> LLMResult:
        """
        重建成 LLMResult 只为复用既有计量入口

        正文不进 llm_calls，所以 content 留空不是丢数据。
        """
        return LLMResult(
            content="",
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            latency_ms=self.latency_ms,
            model_name=model_name,
            finish_reason=self.finish_reason,
            attempts=self.attempts,
        )


@dataclass(frozen=True)
class ChunkProgress:
    """分块进度快照，给 status 轮询显示"已批 M/N 块" """

    total: int
    done: int


def derive_pg_dsn(database_url: str) -> str:
    """
    把 SQLAlchemy 的 `postgresql+psycopg2://...` 串改成 psycopg3 用的 `postgresql://...`

    SQLAlchemy 写 dialect+driver、psycopg3 看裸 dialect。只剥这一层前缀，剩下的
    user / password / host / port / dbname / sslmode 等都通用。
    """
    if database_url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + database_url[len("postgresql+psycopg2://") :]
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url[len("postgresql+psycopg://") :]
    return database_url


async def _read_progress_with(
    checkpointer: _AsyncCheckpointer, thread_id: str
) -> ChunkProgress | None:
    """共用读路径：单测和真库都走这一份实现，差别只在传进来的 saver"""
    snapshot = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if snapshot is None:
        return None
    values = (snapshot.checkpoint or {}).get("channel_values") or {}
    plan = values.get("plan") or []
    if not plan:
        return None
    return ChunkProgress(total=len(plan), done=len(values.get("results") or []))


async def read_progress(thread_id: str) -> ChunkProgress | None:
    """
    生产入口：开一条短连接的 AsyncPostgresSaver 读分块进度。读不到就返回 None，让前端不显示
    骗人的 0/0。

    两个如实的限制：
    - `plan` 节点跑完之前没有块数，前端这段时间只能看到"处理中"；
    - 检查点按 super-step 落盘，扇出里某块抛异常会让整步写入作废，所以失败重跑时
      `done` 可能回落到 0。这不是丢数据（按块缓存在 store 里，钱不会重复烧），
      但数字确实会跳，别在这里编一个更好看的版本。
    """
    dsn = derive_pg_dsn(get_settings().database_url)
    try:
        async with _open_pg_conn(dsn) as conn:
            saver = AsyncPostgresSaver(conn)
            await saver.setup()
            return await _read_progress_with(saver, thread_id)
    except Exception as e:  # 表还没建、连接断了、或 saver 抛任何底层异常
        logger.warning("读分块进度失败 thread_id=%s：%s", thread_id, str(e)[:200])
        return None


@dataclass(frozen=True)
class LongdocResult:
    """整篇批完的产出：拼好的修订文、全局偏移的意见清单、逐块计量"""

    revised_text: str
    issues: list[dict[str, Any]]
    degraded: bool
    chunk_count: int
    cached_chunks: int
    calls: tuple[ChunkCall, ...]
    # 各块召回条款的并集（阶段 6）。按 (doc_id, seq) 去重后保留最高分——
    # 同一条款被三块都命中只算一次，否则引用清单会被重复项刷满
    refs: tuple[dict[str, Any], ...] = ()
    # 任一块的检索降级说明。有就说明这次 RAG 没完全生效，不能当成"没引用就是没问题"
    spec_note: str | None = None


def _merge_refs(results: list[ChunkResult]) -> tuple[dict[str, Any], ...]:
    """跨块合并引用清单：同一条款留最高分，按分数降序"""
    best: dict[tuple[int, int], dict[str, Any]] = {}
    for r in results:
        for ref in r.get("refs") or []:
            key = (ref.get("doc_id"), ref.get("seq"))
            if key not in best or ref.get("score", 0) > best[key].get("score", 0):
                best[key] = ref
    return tuple(sorted(best.values(), key=lambda x: x.get("score", 0), reverse=True))


def _fingerprint(text: str, instruction: str, model_name: str) -> str:
    """
    缓存命中的条件：块文本、批改指令、模型名三者一致

    少了后两个的话，换了 prompt 或换了提供商还在吃旧结果——那种"看起来变好了"
    其实是缓存，比 bug 更难查。
    """
    digest = hashlib.sha256()
    for part in (text, instruction, model_name):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def _build_graph(
    provider: ModelProvider,
    instruction: str,
    max_parallel: int,
    store: _AsyncStore,
    thread_id: str,
    spec_lookup: SpecLookup | None = None,
):
    """
    provider、instruction、store 都用闭包传进节点，状态里只留可序列化内容

    状态里塞 ORM 对象或连接的话检查点直接写不下去，这是扇出上图最容易踩的第一脚。
    `spec_lookup` 同理（阶段 6 按块召回规范条款）：节点自己不查库，查询能力由调用方
    闭包传进来——这样节点仍然可以用假 lookup 单测，也不会出现跨线程共用 session。
    """
    semaphore = asyncio.Semaphore(max_parallel)
    namespace = ("longdoc", thread_id)

    async def plan(state: GradingState) -> dict[str, Any]:
        source = state["source"]
        budget = get_settings().longdoc_chunk_chars
        chunks = split_document(source, max_chars=budget)
        return {
            "plan": [
                {"index": c.index, "start": c.start, "end": c.end, "text": c.text} for c in chunks
            ]
        }

    def fan_out(state: GradingState) -> list[Send]:
        return [Send("grade_chunk", payload) for payload in state["plan"]]

    async def _cached_result(payload: ChunkPayload) -> ChunkResult | None:
        fp = _fingerprint(payload["text"], instruction, provider.model_name)
        item = await store.aget(namespace, str(payload["index"]))
        if item is None or item.value.get("fingerprint") != fp:
            return None
        stored = item.value.get("result") or {}
        if not stored:
            return None
        return {**stored, "index": payload["index"], "cached": True}

    async def grade_chunk(payload: ChunkPayload) -> dict[str, Any]:
        hit = await _cached_result(payload)
        if hit is not None:
            # 命中缓存的块本轮没外呼，调用方据此跳过 llm_calls，别重复计成本
            return {"results": [hit]}

        async with semaphore:
            # 条款按**块**查询而不是整篇查一次：长文档各段涉及的条款不同，
            # 一份全局 top-k 等于只按开头几段做检索（bge 还会静默截断更长的输入）
            context, refs, spec_note = "", [], None
            if spec_lookup is not None:
                context, refs, spec_note = await spec_lookup(payload["text"])
            prompt = instruction.replace("{{content}}", payload["text"])
            result, parsed = await chat_json(
                build_messages(prompt, context), provider, json_schema=GRADING_JSON_SCHEMA
            )
        grading = parse_grading_output(result.content, parsed, payload["text"])

        # 块内偏移在这里就换成整篇偏移。start 和 end 要一起平移——只移 start 的话
        # 第 1 块恰好是恒等偏移，测试里要到第 2 块才露馅
        def _globalize(issue: Any) -> dict[str, Any]:
            dumped = issue.model_dump()
            return {
                **dumped,
                "start": _to_global(dumped["start"], payload["start"]),
                "end": _to_global(dumped["end"], payload["start"]),
            }

        outcome: ChunkResult = {
            "index": payload["index"],
            "revised": grading.revised_text,
            "issues": [_globalize(i) for i in grading.issues],
            "degraded": grading.degraded,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "latency_ms": result.latency_ms,
            "attempts": result.attempts,
            "tier": result.format_tier,
            "finish_reason": result.finish_reason,
            "cached": False,
            "refs": refs,
            "spec_note": spec_note,
        }
        await store.aput(
            namespace,
            str(payload["index"]),
            {
                "fingerprint": _fingerprint(payload["text"], instruction, provider.model_name),
                "result": outcome,
            },
        )
        return {"results": [outcome]}

    async def merge(state: GradingState) -> dict[str, Any]:
        results = sorted(state["results"], key=lambda r: r["index"])
        chunks = [
            Chunk(index=p["index"], start=p["start"], end=p["end"], text=p["text"])
            for p in state["plan"]
        ]
        revised = reassemble(state["source"], chunks, [r["revised"] for r in results])
        issues = [issue for r in results for issue in r["issues"]]
        return {
            "revised": revised,
            "issues": issues,
            "degraded": any(r["degraded"] for r in results),
        }

    builder = StateGraph(GradingState)
    builder.add_node("plan", plan)
    builder.add_node("grade_chunk", grade_chunk)
    builder.add_node("merge", merge)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", fan_out, ["grade_chunk"])
    builder.add_edge("grade_chunk", "merge")
    builder.add_edge("merge", END)
    return builder


def _to_global(local: int | None, base: int) -> int | None:
    """块内偏移 → 整篇偏移。定位失败的一律留 None，前端显示"未定位"而不是猜"""
    return None if local is None else local + base


async def _grade_with_backend(
    source: str,
    provider: ModelProvider,
    instruction: str,
    *,
    thread_id: str,
    checkpointer: Any,
    store: _AsyncStore,
    spec_lookup: SpecLookup | None = None,
) -> LongdocResult:
    """
    共用图执行路径：把已经构造好的 checkpointer / store 传进来跑图

    单测走这条传 AsyncSqliteSaver + AsyncSqliteStore；生产走 `grade_long_document`，
    它先用 Postgres 版本建好再调到这里。
    """
    settings = get_settings()
    max_parallel = min(_MAX_INFLIGHT_HINT, max(1, int(settings.llm_max_concurrency)))
    builder = _build_graph(provider, instruction, max_parallel, store, thread_id, spec_lookup)
    graph = builder.compile(checkpointer=checkpointer)
    final = await graph.ainvoke(
        {"source": source},
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 100},
    )

    results = sorted(final.get("results", []), key=lambda r: r["index"])
    if not results:
        raise LLMError("分块后没有可批改的内容")

    return LongdocResult(
        revised_text=final.get("revised", ""),
        issues=final.get("issues", []) or [],
        degraded=bool(final.get("degraded", True)),
        chunk_count=len(results),
        cached_chunks=sum(1 for r in results if r.get("cached")),
        calls=tuple(
            ChunkCall(
                index=r["index"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                total_tokens=r["total_tokens"],
                latency_ms=r["latency_ms"],
                attempts=r["attempts"],
                tier=r.get("tier"),
                finish_reason=r.get("finish_reason"),
                degraded=bool(r.get("degraded")),
                cached=bool(r.get("cached")),
            )
            for r in results
        ),
        refs=_merge_refs(results),
        spec_note=next((r.get("spec_note") for r in results if r.get("spec_note")), None),
    )


async def grade_long_document(
    source: str,
    provider: ModelProvider,
    instruction: str,
    *,
    thread_id: str,
    spec_lookup: SpecLookup | None = None,
) -> LongdocResult:
    """
    生产入口：分块批改一篇长文档，检查点和按块缓存都落到 Postgres。

    `thread_id` 用 `log-{log_id}`：同一日志重跑会命中同一条时间线，
    已成功的外呼直接读缓存返回，不再花钱。真要从头再批一次，换个 thread_id。

    `spec_lookup` 是阶段 6 的按块条款查询回调（见 `_build_graph`）。不传就是
    不注入规范，行为与阶段 6 之前一致——长文档路径不该因为规范库不可用而变差。

    两个 saver/store 各持一条 psycopg 连接（同一 DSN），互不打扰；
    阶段 4 起不再用 SQLite 文件，checkpoints 和 store 表都在业务库的 `public` schema 里。
    """
    dsn = derive_pg_dsn(get_settings().database_url)
    async with _open_pg_conn(dsn) as cp_conn, _open_pg_conn(dsn) as st_conn:
        checkpointer = AsyncPostgresSaver(cp_conn)
        store = AsyncPostgresStore(st_conn)
        await checkpointer.setup()
        await store.setup()
        return await _grade_with_backend(
            source,
            provider,
            instruction,
            thread_id=thread_id,
            checkpointer=checkpointer,
            store=store,
            spec_lookup=spec_lookup,
        )
