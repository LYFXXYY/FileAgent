"""
VLM 版面检查循环（feature-2 片3）——只修版面，文字一字不动。

【为什么需要】几何模型保证不了的（字体度量差异导致的溢出、窄框里拉丁词
逐字竖排、大面积失衡）只有"渲染出来看一眼"才能发现。这个循环就是那只眼：
渲染幻灯片 → VLM 只评版面 → 按点名结果调渲染器的确定性旋钮 → 重渲染。

【修订旋钮（刻意极小）】
- 被点名的 A 整页忠实页 → 该源页强制改走 B 流式重排（重排天然消压盖/堆叠）。
- 被点名的 B 流式页 → 全局字号缩 0.85（下限 0.7），缓解溢出与行数错估。
旋钮之外什么都不动——**文字内容在整条链路里没有任何被改写的路径**，
这是用户拍板的边界（"只查/修版面，不改文字"）。

【失败哲学（同 §3.7/§3.11）】批评外呼挂了或产出解析不了：
degraded 记账、**绝不把"没听清"当"通过"**，但也无可行动修订 → 交付当前版本。
整个循环是旁路：任何异常都不允许把已经能交付的转换判成 failed。

【Java 对比】类似 Reviewer 装饰器 + 有界重试的 Pipeline：
每轮 Render → Review → Revise，maxRounds 兜底，评审失败降级放行原产物。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from fastapi.concurrency import run_in_threadpool

from services.llm import LLMError, LLMResult, chat_json
from services.ppt_convert import render_blocks_with_meta
from services.pptx_renderer import render_pptx_to_images

logger = logging.getLogger("fileagent.ppt_layout")

# 语义 = "批评 VLM 调用次数"（§3.11 的约定延续）；pass 早停时实际只跑 1 次
DEFAULT_MAX_ROUNDS = 2
HARD_MAX_ROUNDS = 5
# 单轮最多送样的幻灯片数：36 页全送是纯浪费，均匀抽样 + 首尾必送
MAX_SLIDES_PER_ROUND = 12
# 字号缩放步长与下限（下限防"缩到看不见"这种自伤式修订）
_FONT_STEP = 0.85
_FONT_FLOOR = 0.7

LAYOUT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "needs_revision"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slide_index": {"type": "integer"},
                    "category": {
                        "type": "string",
                        "enum": ["overlap", "overflow", "stacked", "too_small", "blank", "other"],
                    },
                    "description": {"type": "string"},
                },
                "required": ["slide_index", "category", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "issues"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "你是幻灯片版面质检员。给你若干张渲染后的幻灯片截图（按阅读顺序编号，编号从 0 起）。"
    "只检查版面问题：文字互相压盖、文字溢出边框、单词逐字母竖排堆叠、字号过小不可读、"
    "大面积失衡空白。你无权也绝不建议修改任何文字内容——修订只允许调整布局。"
    "版面可接受时 verdict 给 pass；否则 needs_revision 并列出问题页。"
    '只输出 JSON：{"verdict": "...", "issues": [{"slide_index": 0, "category": "...",'
    ' "description": "..."}]}'
)


@dataclass(frozen=True)
class LayoutCall:
    """一轮批评外呼的记账原料（调用方写 llm_calls，同 llm 层边界）"""

    result: LLMResult | None
    verdict: str | None = None
    error: str | None = None
    degraded: bool = False
    attempts: int = 1


def _sample_indices(total: int) -> list[int]:
    """均匀抽样至多 MAX_SLIDES_PER_ROUND 张，首尾必送（§3.13 滑窗同理）"""
    if total <= MAX_SLIDES_PER_ROUND:
        return list(range(total))
    step = (total - 1) / (MAX_SLIDES_PER_ROUND - 1)
    return sorted({round(i * step) for i in range(MAX_SLIDES_PER_ROUND)})


def _build_critique_messages(slide_pngs: dict[int, bytes], log_id: int, round_no: int) -> list:
    # 标记里的 thread 跨轮稳定（假端点按它计数），轮次写在标记外
    header = (
        f"[ppt-layout-thread:log-{log_id}] 第 {round_no} 轮检查。"
        f"以下是渲染后的幻灯片截图，编号从 0 起：{sorted(slide_pngs)}。请只评版面。"
    )
    content: list[dict] = [{"type": "text", "text": header}]
    for idx in sorted(slide_pngs):
        content.append({"type": "text", "text": f"幻灯片 {idx}："})
        url = "data:image/png;base64," + base64.b64encode(slide_pngs[idx]).decode()
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": content}]


def _apply_revisions(
    issues: list, meta: list[dict], overrides: dict[int, str], font_scale: float
) -> tuple[dict[int, str], float, bool]:
    """把被点名的幻灯片映射到旋钮。返回 (新 overrides, 新 font_scale, 有无改动)。"""
    new_overrides = dict(overrides)
    new_scale = font_scale
    changed = False
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        try:
            idx = int(issue.get("slide_index", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(meta):
            continue
        entry = meta[idx]
        if entry["mode"] == "page":
            for p in entry["pages"]:
                new_overrides[p] = "flow"
                changed = True
        elif new_scale > _FONT_FLOOR:
            new_scale = max(_FONT_FLOOR, round(new_scale * _FONT_STEP, 3))
            changed = True
    return new_overrides, new_scale, changed


async def run_layout_loop(
    pages: list,
    images: dict,
    page_sizes: list,
    provider: Any,
    log_id: int,
    *,
    first_data: bytes,
    first_meta: list[dict],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    render=None,
) -> tuple[bytes, list[dict], list[LayoutCall]]:
    """批评循环。返回 (最终 pptx 字节, 最终 meta, 外呼清单)。

    render 参数是测试注入点（默认 services.pptx_renderer.render_pptx_to_images）。
    """
    render_fn = render or render_pptx_to_images
    if max_rounds > HARD_MAX_ROUNDS:
        logger.warning("版面循环 max_rounds=%s 超硬上限，钳到 %s", max_rounds, HARD_MAX_ROUNDS)
        max_rounds = HARD_MAX_ROUNDS
    data, meta = first_data, first_meta
    overrides: dict[int, str] = {}
    font_scale = 1.0
    calls: list[LayoutCall] = []
    if max_rounds <= 0:
        return data, meta, calls

    for round_no in range(1, max_rounds + 1):
        slide_count = len(meta)
        try:
            # soffice 真渲染是分钟级阻塞（§3.3），必须丢线程池
            all_pngs = await run_in_threadpool(render_fn, data, slide_count)
        except Exception as e:
            # 渲染后端挂了也是旁路失败：交付当前版本
            logger.warning("版面循环渲染失败 log_id=%s：%r", log_id, e)
            calls.append(LayoutCall(result=None, error=f"渲染失败：{e}", degraded=True))
            break
        sampled = _sample_indices(len(all_pngs))
        messages = _build_critique_messages({i: all_pngs[i] for i in sampled}, log_id, round_no)
        try:
            result, payload = await chat_json(messages, provider, json_schema=LAYOUT_JSON_SCHEMA)
        except LLMError as e:
            calls.append(LayoutCall(result=None, error=str(e), attempts=e.attempts))
            break
        if not isinstance(payload, dict) or payload.get("verdict") not in (
            "pass",
            "needs_revision",
        ):
            # "没听清"绝不当前提通过（§3.11 的边界原样继承）；也无可行动修订 → 收手
            calls.append(LayoutCall(result=result, degraded=True))
            logger.info("log_id=%s 版面批评产出不可解析，按降级交付当前版本", log_id)
            break
        verdict = payload["verdict"]
        issues = payload.get("issues") or []
        calls.append(LayoutCall(result=result, verdict=verdict))
        if verdict == "pass":
            break
        if round_no == max_rounds:
            # 最后一轮仍是 needs_revision：不再重渲染白烧一轮，交付当前版
            logger.info("log_id=%s 版面循环到轮次上限仍有意见，交付当前版本", log_id)
            break
        overrides, font_scale, changed = _apply_revisions(issues, meta, overrides, font_scale)
        if not changed:
            break
        data, meta = await run_in_threadpool(
            render_blocks_with_meta,
            pages,
            images,
            page_sizes,
            overrides=overrides,
            font_scale=font_scale,
        )
    return data, meta, calls
