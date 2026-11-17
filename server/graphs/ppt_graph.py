"""
PPT 生成图（阶段 5 s5.6）—— 大纲 → 拼装 → 渲染 → VLM 批评 → （修订？→ 重拼装）

【当前形状】

    START → plan_slides → assemble → render → critique
                                                       ↓ (verdict==pass OR round>=max)
                                                      END
                                                       ↓ (else)
                                                    revise → assemble (loop)

s5.2 时只有 plan_slides 单节点。本节把它扩成完整批评循环。

【max_rounds 语义】
"批评 LLM 调用次数"上限。默认 2 意味着最多 2 次挑刺 + 1 次 revise；
第 2 次仍 needs_revision 时不再 revise 也不再多挑，交付当前稿并把 verdict 标 needs_revision。
硬上限 5（评测台 A/B 调参时手动放大，正常用户用不到）。

【不接检查点 / 不接 store】
s5.4 渲染接通后才有"重跑不重复烧 VLM"的省钱诉求；现在批评一轮 = VLM 外呼一次 +
revise = 文本 LLM 一次，整图跑一次最多几毛。thread_id 在 config 里**预留传**，
s5.4 切 LibreOffice 时把 AsyncPostgresSaver 接上即可，调用方接口不变。

【metering】
每个 LLM 调用都进 PptRunResult.calls。调用方写 llm_calls 表。purpose 取值：
- outline：plan_slides / revise_outline 这类"产大纲"的外呼
- outline_degraded：parse_outline 走到 fallback 阶梯
- critic：VLM 批评外呼
- critic_degraded：批评 schema 不合形
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from models.provider import ModelProvider
from schemas.ppt_outline import (
    PPT_OUTLINE_JSON_SCHEMA,
    PptOutline,
    parse_outline_output,
)
from schemas.vlm_critique import VLM_CRITIQUE_JSON_SCHEMA, parse_critique
from services.llm import LLMError, LLMResult, chat_json
from services.ppt_outline import build_outline_messages
from services.pptx_builder import build_pptx
from services.pptx_renderer import render_pptx_to_images
from services.vlm_critic import build_critic_messages, build_revise_messages

logger = logging.getLogger("fileagent.graph.ppt")

# 评测台调参时手动调；用户层不让它超 5（成本护栏）
_HARD_MAX_ROUNDS = 5
_DEFAULT_MAX_ROUNDS = 2


class PptCallData(TypedDict, total=False):
    """单步外呼的计量数据（图状态里只放标量，不放 LLMResult 实例——msgpack 写不下去）"""

    phase: str  # "plan_slides" / "critique" / "revise_outline"
    round: int
    degraded: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    attempts: int
    tier: str | None
    finish_reason: str | None
    model_name: str


class PptState(TypedDict, total=False):
    """图状态。calls 用 append reducer；同 thread 不接检查点时单次跑只追加不重复"""

    source: str
    instruction: str
    outline_payload: dict[str, Any]
    pptx_bytes: bytes
    page_count: int
    slide_images: list[bytes]
    last_critique_payload: dict[str, Any] | None
    last_verdict: str
    round_count: int
    max_rounds: int
    degraded: bool
    calls: Annotated[list[PptCallData], lambda a, b: a + b]


@dataclass(frozen=True)
class PptCall:
    """回写给调用方写 llm_calls 用的不可变记录"""

    phase: str
    round: int
    degraded: bool
    result: LLMResult

    @property
    def purpose(self) -> str:
        if self.phase == "critique":
            return "critic_degraded" if self.degraded else "critic"
        return "outline_degraded" if self.degraded else "outline"


@dataclass(frozen=True)
class PptRunResult:
    """整图跑完的产出。outline 是终稿，pptx_bytes 是最后一次 assemble 的产物"""

    outline: PptOutline
    pptx_bytes: bytes
    verdict: str  # "pass" / "needs_revision" / "skipped"（无 critic_provider 时）
    rounds: int
    degraded: bool
    calls: tuple[PptCall, ...] = field(default_factory=tuple)


def _snapshot_result(
    result: LLMResult,
    *,
    phase: str,
    round_index: int,
    degraded: bool,
) -> PptCallData:
    return {
        "phase": phase,
        "round": round_index,
        "degraded": degraded,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "latency_ms": result.latency_ms,
        "attempts": result.attempts,
        "tier": result.format_tier,
        "finish_reason": result.finish_reason,
        "model_name": result.model_name,
    }


def _build_graph(
    provider: ModelProvider,
    critic_provider: ModelProvider | None,
    max_rounds: int,
    thread_id: str,
):
    """
    provider 闭包传进来，节点不查库（§3.6 / §3.10 边界）

    critic_provider=None 时 critique 节点直接 pass，不进 VLM 外呼——s5.2 行为保持不变。
    thread_id 也用闭包传：state 里塞了反而会让 checkpoint 写入多一帧不变字段，浪费。
    """

    async def plan_slides(state: PptState) -> dict[str, Any]:
        prompt = state["instruction"].replace("{{content}}", state["source"])
        result, parsed = await chat_json(
            build_outline_messages(prompt), provider, json_schema=PPT_OUTLINE_JSON_SCHEMA
        )
        outline = parse_outline_output(result.content, parsed)
        return {
            "outline_payload": outline.model_dump(),
            "round_count": 0,
            "max_rounds": max_rounds,
            "degraded": outline.degraded,
            "calls": [
                _snapshot_result(
                    result,
                    phase="plan_slides",
                    round_index=0,
                    degraded=outline.degraded,
                )
            ],
        }

    def assemble(state: PptState) -> dict[str, Any]:
        outline = PptOutline(**state["outline_payload"])
        pptx = build_pptx(outline, image_refs=None)
        return {"pptx_bytes": pptx, "page_count": len(outline.slides) + 1}

    async def render(state: PptState) -> dict[str, Any]:
        """
        渲染整份 PPTX 为逐页 PNG，产出进 state 给 critique 用

        soffice 后端一次要 3–8 秒，sync 调用会卡住整个事件循环（单 worker 部署下
        连 health 都点不动），所以走 asyncio.to_thread。stub 后端瞬时，同一路径无成本。
        """
        images = await asyncio.to_thread(
            render_pptx_to_images, state["pptx_bytes"], state.get("page_count", 1)
        )
        return {"slide_images": images}

    async def critique(state: PptState) -> dict[str, Any]:
        round_count = state.get("round_count", 0)

        if critic_provider is None:
            # 没配 vision 提供商——跳过 VLM，标 pass 让循环退出
            return {
                "last_verdict": "pass",
                "last_critique_payload": None,
                "round_count": round_count + 1,
            }

        # 真/占位渲染由 settings.ppt_render_backend 派发（§3.11：只换 renderer 实现）
        slide_images = state.get("slide_images") or []
        thread_marker = f"[critic-thread:{thread_id}]"
        messages = build_critic_messages(
            state["outline_payload"], slide_images, marker=thread_marker
        )
        result, parsed = await chat_json(
            messages, critic_provider, json_schema=VLM_CRITIQUE_JSON_SCHEMA
        )
        crit = parse_critique(result.content, parsed)
        return {
            "last_verdict": crit.verdict,
            "last_critique_payload": crit.model_dump(),
            "round_count": round_count + 1,
            "calls": [
                _snapshot_result(
                    result,
                    phase="critique",
                    round_index=round_count + 1,
                    degraded=crit.degraded,
                )
            ],
        }

    def should_revise(state: PptState) -> str:
        verdict = state.get("last_verdict", "pass")
        rounds = state.get("round_count", 0)
        cap = state.get("max_rounds", _DEFAULT_MAX_ROUNDS)
        if verdict == "pass":
            return "end"
        if rounds >= cap:
            return "end"
        return "revise"

    async def revise_outline(state: PptState) -> dict[str, Any]:
        critique_payload = state.get("last_critique_payload") or {}
        messages = build_revise_messages(state["outline_payload"], critique_payload)
        result, parsed = await chat_json(messages, provider, json_schema=PPT_OUTLINE_JSON_SCHEMA)
        outline = parse_outline_output(result.content, parsed)
        return {
            "outline_payload": outline.model_dump(),
            "degraded": bool(state.get("degraded", False)) or outline.degraded,
            "calls": [
                _snapshot_result(
                    result,
                    phase="revise_outline",
                    round_index=state.get("round_count", 0),
                    degraded=outline.degraded,
                )
            ],
        }

    builder = StateGraph(PptState)
    builder.add_node("plan_slides", plan_slides)
    builder.add_node("assemble", assemble)
    builder.add_node("render", render)
    builder.add_node("critique", critique)
    builder.add_node("revise_outline", revise_outline)

    builder.add_edge(START, "plan_slides")
    builder.add_edge("plan_slides", "assemble")
    builder.add_edge("assemble", "render")
    builder.add_edge("render", "critique")
    builder.add_conditional_edges(
        "critique",
        should_revise,
        {"revise": "revise_outline", "end": END},
    )
    builder.add_edge("revise_outline", "assemble")
    return builder


async def run_ppt_pipeline(
    source: str,
    provider: ModelProvider,
    instruction: str,
    *,
    thread_id: str,
    critic_provider: ModelProvider | None = None,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
) -> PptRunResult:
    """
    生产入口：跑完整 PPT 图，返回终稿大纲 + PPTX 二进制 + 全部外呼记录

    Args:
        provider: 文本/大纲提供商（capabilities 含 text）
        critic_provider: 视觉批评提供商（capabilities 含 vision）；None 时批评环节跳过
        max_rounds: 批评 LLM 调用上限。硬上限 5，超过会钳到 5
    """
    if max_rounds > _HARD_MAX_ROUNDS:
        logger.warning("PPT 批评循环 max_rounds=%s 超硬上限，钳到 %s", max_rounds, _HARD_MAX_ROUNDS)
        max_rounds = _HARD_MAX_ROUNDS

    builder = _build_graph(provider, critic_provider, max_rounds, thread_id)
    graph = builder.compile()
    final = await graph.ainvoke(
        {"source": source, "instruction": instruction},
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 30},
    )

    payload = final.get("outline_payload") or {}
    if not payload.get("slides"):
        raise LLMError("PPT 流水线终态里 slides 为空")
    outline = PptOutline(**payload)

    calls = tuple(
        PptCall(
            phase=c["phase"],
            round=c["round"],
            degraded=c["degraded"],
            result=LLMResult(
                content="",
                prompt_tokens=c["prompt_tokens"],
                completion_tokens=c["completion_tokens"],
                total_tokens=c["total_tokens"],
                latency_ms=c["latency_ms"],
                model_name=c["model_name"],
                finish_reason=c.get("finish_reason"),
                attempts=c.get("attempts", 1),
                format_tier=c.get("tier"),
            ),
        )
        for c in final.get("calls", [])
    )

    if critic_provider is None:
        # critique 节点直接 pass，但语义上是"没批评可考"，调用方要看 skipped 区分
        verdict = "skipped"
    else:
        verdict = final.get("last_verdict") or "pass"

    return PptRunResult(
        outline=outline,
        pptx_bytes=final.get("pptx_bytes") or b"",
        verdict=verdict,
        rounds=int(final.get("round_count") or 0),
        degraded=bool(final.get("degraded", False)),
        calls=calls,
    )


async def plan_ppt_outline(
    source: str,
    provider: ModelProvider,
    instruction: str,
    *,
    thread_id: str,
) -> tuple[PptOutline, LLMResult | None]:
    """
    s5.2 留下的兼容入口：不带批评循环的单步大纲

    内部走 run_ppt_pipeline 的 critic_provider=None 路径，返回形状仍按 s5.2 契约。
    s5.6 之后调用方应直接走 run_ppt_pipeline；这条仅为不破坏 s5.2 单测保留。
    """
    result = await run_ppt_pipeline(
        source,
        provider,
        instruction,
        thread_id=thread_id,
        critic_provider=None,
        max_rounds=1,
    )
    plan_call = next((c for c in result.calls if c.phase == "plan_slides"), None)
    return result.outline, plan_call.result if plan_call else None


# 让 json 模块在本文件 lint 校验下保留（critique payload 调试用）
_ = json
