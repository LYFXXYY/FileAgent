"""
PPT 大纲结构化输出契约（阶段 5 s5.2）

【设计约束】
- 输出形状严格：`{title, slides: [{title, bullets, image_placeholders}]}`。
  阶段 5 永远只填文字，但 `image_placeholders` 字段必须**现在就存在**——
  SPEC §9.12.2.C 把它定为"图片轮切换实现时不改外部接口"的硬约束。
- 解析失败必须降级而不是 fail：模型吐自由文本、缺字段、被围栏包裹都是常态。
  `parse_outline_output` 退回"按换行切行 + 每行当一页标题 + 无 bullets"，degraded=True。
  用户的文档必须拿到一个结果。
- 落库 JSON 顶层带 `kind: "ppt_outline"` 区分批改类，`is_degraded` 沿用同一字段名。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

# 给 chat_json 用的严格 schema。字段名必须和 PptOutline / PptSlide 一致，改一边改另一边
PPT_OUTLINE_JSON_SCHEMA: dict = {
    "name": "ppt_outline",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {
                "type": "string",
                "description": "整篇幻灯片的总标题，从文档大标题或首段提炼，没有就空串",
            },
            "slides": {
                "type": "array",
                "description": "幻灯片页列表，按文档段落顺序排，至少 1 页",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "本页标题，不超过 30 字",
                        },
                        "bullets": {
                            "type": "array",
                            "description": "本页要点，每条不超过 60 字；纯标题页可为空数组",
                            "items": {"type": "string"},
                        },
                        "image_placeholders": {
                            "type": "array",
                            "description": (
                                "阶段 5 永远传空数组。字段名是图片轮的扩展点：将来放 PDF 抽出的图，"
                                "元素是占位符 ID（如 'fig_3_1'）。当前不要往里写东西。"
                            ),
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["title", "bullets", "image_placeholders"],
                },
            },
        },
        "required": ["title", "slides"],
    },
}


class PptSlide(BaseModel):
    title: str = Field(min_length=1)
    bullets: list[str] = Field(default_factory=list)
    # 阶段 5 永远是空 list；字段存在是为图片轮不动 schema
    image_placeholders: list[str] = Field(default_factory=list)


class PptOutline(BaseModel):
    title: str = ""
    slides: list[PptSlide] = Field(default_factory=list)
    degraded: bool = False


def parse_outline_output(
    raw_text: str,
    parsed_json: dict[str, Any] | None,
) -> PptOutline:
    """
    把模型输出转成 PptOutline。任何不合形的输入都要降级返回，绝不抛

    三条降级阶梯：
    1. parsed_json 是 schema 形状 → 正常解析（degraded=False）
    2. parsed_json 缺 slides / slides 不是 list → 拿 raw_text 走纯文本阶梯
    3. raw_text 是自由文本 → 按换行切行，每行当一页标题，bullets 留空
    """
    if parsed_json is not None and isinstance(parsed_json.get("slides"), list):
        try:
            slides = [PptSlide(**s) for s in parsed_json["slides"]]
        except Exception:
            # 任何一页元素结构不对（缺 title / bullets 不是 list）都退回阶梯 2/3
            return _fallback_from_text(raw_text)
        if not slides:
            return _fallback_from_text(raw_text)
        return PptOutline(
            title=str(parsed_json.get("title") or ""),
            slides=slides,
            degraded=False,
        )

    return _fallback_from_text(raw_text)


def _fallback_from_text(raw_text: str) -> PptOutline:
    """
    阶梯 2/3：模型没吐 schema 形状就按纯文本硬切

    每行当一页标题；超过 60 字的截到 60 字（避免一行 1000 字的"标题"把 PPT 撑爆）；
    空行直接跳。一行都没有就建一页"无法解析"占位，绝不让 slides 是空 list。
    """
    cleaned = (raw_text or "").strip()
    if not cleaned:
        return PptOutline(
            title="",
            slides=[PptSlide(title="（模型未返回有效内容）")],
            degraded=True,
        )

    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    slides = [PptSlide(title=ln[:60]) for ln in lines]
    return PptOutline(
        title=slides[0].title if len(slides) > 0 else "",
        # 阶梯 3 没区分总标题和首页，硬复用第一行；阶段 5 mock VLM 反馈循环里
        # 这条会被 prompt 修订掉，先保证不空
        slides=slides,
        degraded=True,
    )


def serialize_outline(outline: PptOutline) -> str:
    """落库 JSON。顶层带 kind 区分批改类，is_degraded 沿用同字段名"""
    payload = {
        "kind": "ppt_outline",
        "degraded": outline.degraded,
        "title": outline.title,
        "slides": [s.model_dump() for s in outline.slides],
    }
    return json.dumps(payload, ensure_ascii=False)


def deserialize_outline(stored: str | None) -> PptOutline | None:
    """
    读出 PptOutline。非 ppt_outline 的 structured_output（批改类）返回 None

    阶段 5 s5.3 下载路由会调它——把 outline 喂给 PPTX builder。
    """
    if not stored:
        return None
    try:
        data = json.loads(stored)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("kind") != "ppt_outline":
        return None
    try:
        slides = [PptSlide(**s) for s in data.get("slides", [])]
    except Exception:
        return None
    return PptOutline(
        title=str(data.get("title") or ""),
        slides=slides,
        degraded=bool(data.get("degraded")),
    )
