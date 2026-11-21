"""
VLM 坏块修复（feature-2 片2）

【要解决什么】MinerU 偶尔把块解析坏：段落/标题文本为空、表格 html 缺失或畸形。
片1 对这些块只能回落占位文本/整表渲染图——信息在，但不可编辑也读不了。
片2 把坏块所在区域从**原 PDF 裁出真实图片**喂给视觉模型逐字转录，
修复后的块打上 `repaired` 标记，渲染器据此在产物里标注"LLM 修复"。

【防幻觉边界（用户拍板）】
- 必须以区域原图为 grounding：没有图就不修，绝不靠上下文"猜"内容。
- 提示词只许逐字转录，不许改写/总结。
- 修复产物要过结构校验（表格必须能解析成网格）才替换原块。

【失败哲学（同 §3.6/§3.15）】修复是旁路增强：VLM 挂了、输出烂了、区域裁不出来，
一律保留原块继续交付，绝不让已经能跑通的转换判失败。
每笔外呼（含失败）都回传给调用方记账——本模块不碰 DB。

【Java 对比】
类似一个可选的 Enrichment Client：输入 DTO 返回新 DTO，
Spring 里对应 @Async + fallback 的装饰器，失败走默认值。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import pymupdf

from services.llm import LLMError, LLMResult, chat_json
from services.ppt_convert import _block_text, parse_html_table

logger = logging.getLogger("fileagent.ppt_repair")

# 单份文档最多修这么多个坏块：坏块成串通常意味着整份解析失败，
# 逐块烧 VLM 不如保留片1 的占位/图兜底，成本闸必须有
MAX_REPAIR_BLOCKS = 20
# 区域裁剪渲染倍率（72dpi × 2 = 144dpi，够 VLM 认字又不至于图太大）
_CROP_ZOOM = 2.0

_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "你是文档转换质检员。用户会给你一张从原 PDF 页面裁出的区域图，"
    "该区域在版面解析时失败了。请逐字转录图中的内容，"
    "不要改写、不要总结、不要添加解释。图中没有内容时输出空字符串。"
)


@dataclass(frozen=True)
class RepairCall:
    """一笔修复外呼的记账原料（调用方负责写 llm_calls，同 llm 层边界）"""

    result: LLMResult | None
    error: str | None = None
    # 调用成功但产出没通过校验/为空——块保持原样，评测与排查要能区分这两种
    degraded: bool = False
    # 失败路径的实际尝试次数（从 LLMError.attempts 来），同 §3.6 的计量边界
    attempts: int = 1


def find_defects(pages: list) -> list[tuple[int, int, str]]:
    """扫出坏块清单 (页号, 块号, 种类)。只认两类确定性缺陷，不做主观判断。

    - text：title/paragraph 的文本为空（MinerU 有块没内容 = 识别失败）
    - table：html 缺失或解析不出网格（片1 只能回落图）
    """
    defects: list[tuple[int, int, str]] = []
    for pi, blocks in enumerate(pages):
        if not isinstance(blocks, list):
            continue
        for bi, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            if kind in ("title", "paragraph"):
                if not _block_text(block).strip():
                    defects.append((pi, bi, "text"))
            elif kind == "table":
                html = (block.get("content") or {}).get("html")
                parsed = parse_html_table(html) if html else None
                if parsed is None:
                    defects.append((pi, bi, "table"))
    return defects


def crop_region(
    pdf_bytes: bytes, page_index: int, bbox: list, page_w: float, page_h: float
) -> bytes | None:
    """按归一化 bbox（0..1000）从原 PDF 页面裁出区域渲染成 PNG。

    页码越界、区域退化（零宽高）返回 None——调用方跳过该块，绝不猜。
    """
    x0, y0, x1, y1 = bbox
    rect = pymupdf.Rect(
        x0 / 1000 * page_w,
        y0 / 1000 * page_h,
        x1 / 1000 * page_w,
        y1 / 1000 * page_h,
    )
    if rect.width < 2 or rect.height < 2:
        return None
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page_index >= len(doc):
            return None
        pix = doc[page_index].get_pixmap(matrix=pymupdf.Matrix(_CROP_ZOOM, _CROP_ZOOM), clip=rect)
        return pix.tobytes("png")
    finally:
        doc.close()


def _user_prompt(kind: str, log_id: int) -> str:
    marker = f"[ppt-repair:log-{log_id}]"
    if kind == "table":
        return (
            f"{marker} 这是一张表格区域图。请输出该表格的 HTML，"
            "用 <table><tr><td rowspan=1 colspan=1>单元格</td></tr></table> 结构，"
            "合并单元格如实写 rowspan/colspan。"
            '只输出 JSON：{"content": "<表格HTML>"}'
        )
    return (
        f"{marker} 这是一段文字区域图。请逐字转录其中的文字，保留原有换行。"
        '只输出 JSON：{"content": "转录的文字"}'
    )


def _apply_repair(block: dict, kind: str, content: str) -> dict | None:
    """把 VLM 产出装配成新块（不可变：整块复制后替换）。没通过校验返回 None。"""
    content = (content or "").strip()
    if not content:
        return None
    new_block = dict(block)
    new_content = dict(block.get("content") or {})
    if kind == "table":
        if "<table" not in content.lower() or parse_html_table(content) is None:
            return None
        new_content["html"] = content
    else:
        text_key = "title_content" if new_block.get("type") == "title" else "paragraph_content"
        new_content[text_key] = [{"type": "text", "content": content}]
    new_block["content"] = new_content
    new_block["repaired"] = True
    return new_block


async def repair_defective_blocks(
    pdf_bytes: bytes,
    pages: list,
    page_sizes: list,
    provider: Any,
    log_id: int,
) -> tuple[list, list[RepairCall]]:
    """修复坏块，返回 (新 pages, 外呼清单)。provider 由调用方解析（vision 能力）。

    只修 MAX_REPAIR_BLOCKS 个；单块失败保留原块。整体不抛——
    转换的兜底交付（占位/图）永远站得住，修复只是增强。
    """
    defects = find_defects(pages)
    if not defects:
        return pages, []
    if len(defects) > MAX_REPAIR_BLOCKS:
        logger.warning(
            "log_id=%s 坏块 %d 个，超上限只修前 %d 个", log_id, len(defects), MAX_REPAIR_BLOCKS
        )
        defects = defects[:MAX_REPAIR_BLOCKS]

    new_pages = [list(page) if isinstance(page, list) else page for page in pages]
    calls: list[RepairCall] = []
    # 同一页多个坏块共用一张裁图不划算（区域各不相同），逐块裁逐块问
    for pi, bi, kind in defects:
        block = new_pages[pi][bi]
        bbox = block.get("bbox")
        if page_sizes and pi < len(page_sizes):
            page_w, page_h = page_sizes[pi]
        else:
            page_w, page_h = 595.0, 842.0
        region = crop_region(pdf_bytes, pi, bbox, page_w, page_h)
        if region is None:
            calls.append(RepairCall(result=None, error="区域裁剪失败", degraded=True))
            continue
        data_url = "data:image/png;base64," + base64.b64encode(region).decode()
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _user_prompt(kind, log_id)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        try:
            result, payload = await chat_json(messages, provider)
        except LLMError as e:
            calls.append(RepairCall(result=None, error=str(e), attempts=e.attempts))
            continue
        content = payload.get("content") if isinstance(payload, dict) else None
        new_block = _apply_repair(block, kind, content or "")
        if new_block is None:
            calls.append(RepairCall(result=result, degraded=True))
            logger.info("log_id=%s 页%d块%d 修复产出未过校验，保留原块", log_id, pi, bi)
            continue
        new_pages[pi][bi] = new_block
        calls.append(RepairCall(result=result))
    return new_pages, calls
