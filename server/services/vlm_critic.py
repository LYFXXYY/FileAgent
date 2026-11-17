"""
VLM 批评 / 修订提示词构造（阶段 5 s5.6）

【和 s5.5 mock 的契约】
批评 messages 的文本部分**必须**带 [critic-thread:log-X] 这一标记——mock 用它按 thread
计数控制 MOCK_VLM_STUB_ROUNDS 退出条件。graph 里 critique 节点拼 prompt 时塞进去。

【messages 形态】
OpenAI 兼容多模态：content 是 list，text 部分给指示，image_url 部分给 base64 PNG。
s5.4 stub 渲染的图也是 base64，VLM 端拿到不报错。
"""

from __future__ import annotations

import base64
from typing import Any

CRITIC_SYSTEM = """\
你是一位严格的演示文稿视觉评审专家。给你一份 PPT 的若干页图像和它的大纲文本，请挑出**任何**
影响阅读的问题，包括：

1. 标题与正文重叠 / 字号过小 / 文字溢出框
2. 行距过密、列表项挤成一团
3. 颜色对比不足（白底白字、深色背景配暗色文字）
4. 同一页要点过多（>6 条），观众来不及读
5. 缺过渡页、章节标题缺失

如果整份看起来都合理，输出 verdict="pass"，issues 为空数组。
有任何问题就 verdict="needs_revision"，逐条指出。

**只输出 JSON**，不要包 ```json 围栏，不要前后写解释。
"""

REVISE_SYSTEM = """\
你把一份演示文稿大纲按视觉评审反馈修订，输出**完整新大纲** JSON。

【修订规则】
1. 只针对反馈里指出的页做改动，其他页原样保留；
2. 不要删页（除非反馈明确指出某页是冗余）；
3. bullets 缩减到 ≤6 条、单条 ≤60 字；
4. 标题压正文类问题：把标题文本缩到 ≤20 字；
5. image_placeholders 数组永远传 []（图片轮启用前不许填）。

**只输出 JSON**，结构与之前一致：{title, slides:[{title, bullets, image_placeholders}]}。
"""


def build_critic_messages(
    outline_payload: dict[str, Any],
    slide_images: list[bytes],
    *,
    marker: str,
) -> list[dict[str, Any]]:
    """
    拼一次 VLM 批评请求

    Args:
        outline_payload: 当前大纲的 dict 形态
        slide_images: 渲染出的每页 PNG 字节。s5.4 之前是 stub 1x1 全透明 PNG
        marker: [critic-thread:...] 字符串，mock 端按它计数
    """
    user_text = _format_outline_for_critique(outline_payload) + f"\n\n{marker}"

    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for img in slide_images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    return [
        {"role": "system", "content": CRITIC_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_revise_messages(
    outline_payload: dict[str, Any],
    critique_payload: dict[str, Any],
) -> list[dict[str, str]]:
    """
    拼一次"按反馈改大纲"的文本外呼

    反馈文本要可读——别让模型只看 JSON 字符串猜哪页出问题；按页码分块列出问题
    """
    issues_text = _format_issues(critique_payload.get("issues") or [])
    user_text = (
        f"原大纲：\n{_format_outline_for_critique(outline_payload)}\n\n"
        f"视觉评审反馈：\n{issues_text}\n\n请输出修订后的完整大纲 JSON。"
    )
    return [
        {"role": "system", "content": REVISE_SYSTEM},
        {"role": "user", "content": user_text},
    ]


def _format_outline_for_critique(outline: dict[str, Any]) -> str:
    title = outline.get("title") or "（无总标题）"
    parts = [f"总标题：{title}"]
    for i, slide in enumerate(outline.get("slides") or [], start=1):
        parts.append(f"第 {i} 页：{slide.get('title') or '（无标题）'}")
        for b in slide.get("bullets") or []:
            parts.append(f"  - {b}")
    return "\n".join(parts)


def _format_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "（无）"
    parts: list[str] = []
    for issue in issues:
        idx = issue.get("slide_index", "?")
        cat = issue.get("category", "other")
        sev = issue.get("severity", "medium")
        desc = issue.get("description", "")
        sug = issue.get("suggestion", "")
        line = f"第 {idx} 页 [{sev}/{cat}] {desc}"
        if sug:
            line += f" → 建议：{sug}"
        parts.append(line)
    return "\n".join(parts)
