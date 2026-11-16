"""
长文档分块批改图（阶段 3）—— LangGraph 首练：扇出扇入 + 检查点续跑

【图形状】

    START → plan → (Send 扇出) → grade_chunk × N → merge → END

`plan` 只算块边界不花钱；`grade_chunk` 是唯一的外呼节点；`merge` 把逐块结果按原位置
拼回一篇，并把每块的局部偏移换成全局偏移（前端按整篇原文高亮，见 §3.7）。

【省钱靠两样，缺一不可，别只留一个】
- **检查点（AsyncSqliteSaver）**：按 super-step 落盘。它管的是"进程在两步之间没了，
  下次从这一步之后接着跑"。**它不管块级失败**——同一个扇出步里有一块抛异常，
  整步的写入一起作废，别的块的结果读不回来（实测过：重跑时三块全都重打）。
- **按块幂等存储（SqliteStore）**：每块成功外呼后立刻把结果写进 store，节点开头先查。
  键是 `(thread_id, chunk_index)`，命中还要求**块文本、指令摘要、模型名**三者一致，
  换提示词或换模型都不会吃到旧结果。这才是"第 99 块挂了别把前 98 块再烧一遍"的根据。

命中的块**不再写 llm_calls**：它这轮没发生外呼，记账进去就是重复计成本。

【这里刻意没有的东西】
- **图级 RetryPolicy**：`chat()` 已经按白名单重试过一轮，再套一层就是把同一个提示
  打两次、双倍烧钱。分层重试只留一层。
- **interrupt 人工确认**：确认"分块方案对不对"要先有 UI（第 2 步预览已经能做），
  图内挂起等确认是下一步的事，别为了用上 API 而制造一次交互。
- **节点里查库**：provider 由调用方解析后用闭包传进来。节点只碰模型，
  才能像 `services/llm.py` 那样用假 client 单测（§3.6 的边界不破）。

【检查点存哪】
`settings.checkpoint_path`（默认 `checkpoints/grading.sqlite3`）存图状态；
按块结果另存一个同目录的 `.chunks.sqlite3`——两个连接写同一个 sqlite 文件会撞锁，
分文件比调 WAL 便宜且可控。`thread_id = log-{log_id}`，一条日志一条时间线。
单 worker 假设跟启动 reaper 一致；多 worker 或长期上线要换成 Postgres saver + Postgres
store——那正是阶段 2.5/4 的既定动作。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Send

from config import BASE_DIR, get_settings
from models.provider import ModelProvider
from schemas.grading import GRADING_JSON_SCHEMA
from services.chunking import Chunk, reassemble, split_document
from services.grading import build_messages, parse_grading_output
from services.llm import LLMError, LLMResult, chat_json

logger = logging.getLogger("fileagent.graph")

# 扇出上限：一次塞几十个分支进图，排到信号闸外面只会白白占内存
_MAX_INFLIGHT_HINT = 8


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


async def read_progress(thread_id: str, checkpoint_path: str | None = None) -> ChunkProgress | None:
    """
    从检查点读分块进度。读不到就返回 None，让前端不显示进度，而不是显示骗人的 0/0。

    两个如实的限制：
    - `plan` 节点跑完之前没有块数，前端这段时间只能看到"处理中"；
    - 检查点按 super-step 落盘，扇出里某块抛异常会让整步写入作废，所以失败重跑时
      `done` 可能回落到 0。这不是丢数据（按块缓存在 store 里，钱不会重复烧），
      但数字确实会跳，别在这里编一个更好看的版本。
    """
    settings = get_settings()
    path = _resolve_dir_file(checkpoint_path or settings.checkpoint_path)

    try:
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            snapshot = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    except Exception as e:  # 文件还没建、被清掉了、或格式不认识
        logger.warning("读分块进度失败 thread_id=%s：%s", thread_id, str(e)[:200])
        return None

    if snapshot is None:
        return None

    values = (snapshot.checkpoint or {}).get("channel_values") or {}
    plan = values.get("plan") or []
    if not plan:
        return None
    return ChunkProgress(total=len(plan), done=len(values.get("results") or []))


@dataclass(frozen=True)
class LongdocResult:
    """整篇批完的产出：拼好的修订文、全局偏移的意见清单、逐块计量"""

    revised_text: str
    issues: list[dict[str, Any]]
    degraded: bool
    chunk_count: int
    cached_chunks: int
    calls: tuple[ChunkCall, ...]


def _resolve_dir_file(path: str) -> str:
    """相对路径按 server/ 解析并确保父目录存在（sqlite 不会自己建目录）"""
    full = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    full = os.path.normpath(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def _chunk_cache_path(checkpoint_path: str) -> str:
    """
    按块结果缓存用同目录的另一个 sqlite 文件

    两个连接写同一个文件会撞库锁，除非我确定两边都开了 WAL——分文件比调 WAL 便宜且可控。
    """
    root, ext = os.path.splitext(checkpoint_path)
    return f"{root}.chunks{ext}"


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
    store: SqliteStore,
    thread_id: str,
):
    """
    provider、instruction、store 都用闭包传进节点，状态里只留可序列化内容

    状态里塞 ORM 对象或连接的话检查点直接写不下去，这是扇出上图最容易踩的第一脚。
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

    def _cached_result(payload: ChunkPayload) -> ChunkResult | None:
        fp = _fingerprint(payload["text"], instruction, provider.model_name)
        item = store.get(namespace, str(payload["index"]))
        if item is None or item.value.get("fingerprint") != fp:
            return None
        stored = item.value.get("result") or {}
        if not stored:
            return None
        return {**stored, "index": payload["index"], "cached": True}

    async def grade_chunk(payload: ChunkPayload) -> dict[str, Any]:
        hit = _cached_result(payload)
        if hit is not None:
            # 命中缓存的块本轮没外呼，调用方据此跳过 llm_calls，别重复计成本
            return {"results": [hit]}

        async with semaphore:
            prompt = instruction.replace("{{content}}", payload["text"])
            result, parsed = await chat_json(
                build_messages(prompt), provider, json_schema=GRADING_JSON_SCHEMA
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
        }
        store.put(
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


async def grade_long_document(
    source: str,
    provider: ModelProvider,
    instruction: str,
    *,
    thread_id: str,
    checkpoint_path: str | None = None,
) -> LongdocResult:
    """
    分块批改一篇长文档，返回可直接铺进 operation_log 的结果。

    `thread_id` 用 `log-{log_id}`：同一日志重跑会命中同一条时间线，
    已成功的外呼直接读缓存返回，不再花钱。真要从头再批一次，换个 thread_id
    （或者删掉这两个 sqlite 文件）。
    """
    settings = get_settings()
    max_parallel = min(_MAX_INFLIGHT_HINT, max(1, int(settings.llm_max_concurrency)))
    ckpt = _resolve_dir_file(checkpoint_path or settings.checkpoint_path)
    cache = _resolve_dir_file(_chunk_cache_path(ckpt))

    # from_conn_string 是同步上下文管理器；SqliteStore 的构造函数要的是 Connection 对象，
    # 传路径进去会一路走到 create_function 才炸，第一次撞的人都栽在这
    with SqliteStore.from_conn_string(cache) as store:
        store.setup()
        builder = _build_graph(provider, instruction, max_parallel, store, thread_id)
        async with AsyncSqliteSaver.from_conn_string(ckpt) as saver:
            graph = builder.compile(checkpointer=saver)
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
    )
