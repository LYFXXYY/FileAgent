"""
OCR 双轨输出层 —— MinerU 产物 → 批改可用的 plain_text + structured 留存

【双轨约定】（AGENTS §7 / SPEC §9.12）
- plain_text：守 §3.8 的"\\n 分隔自然段、与 docx 提取同构"不变式，
  供批改 / diff / `_find_unused_span` 定位消费。**表格第一步不进正文**（用户拍板）：
  拍平规则会引入定位语义的复杂度，表格能力留给 structured 轨后续消费。
- structured：full.md + content_list.json + 图片按 log_id 落盘留存，
  供后续 RAG 规范库扩容 / PPT 带图（image_refs 口子）消费，第一步不接 UI。

【Java 对比】
- 类似一个无状态 Converter + 文件仓储：转换规则是纯函数（可单测），
  落盘是独立动作，两者都不开事务
"""

from __future__ import annotations

import json
import logging
import os
import re

from services.mineru import MinerUResult

logger = logging.getLogger("fileagent.ocr")

# 整行跳过：图片引用、HTML 标签行（MinerU 表格常以 <html><body><table> 包裹）
_IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_HTML_TAG_LINE = re.compile(r"^\s*</?[a-zA-Z][^>]*>\s*$")
# HTML 表格行：MinerU 常把整张表压成一行 <table><tr><td>…，
# 只按"纯标签行"跳会漏掉它，单元格文字就混进批改正文了（表格不进正文是拍板决策）
_HTML_TABLE_LINE = re.compile(r"<\s*(table|tr|td|th)\b", re.IGNORECASE)
# 行内清除：图片、HTML 标签、链接留文字去地址
_IMAGE_INLINE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_INLINE = re.compile(r"</?[a-zA-Z][^>]*>")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# 行首标记：标题 #、无序列表 -/*/+、有序列表 1. / 1)
_HEADING = re.compile(r"^#{1,6}\s+")
_UL = re.compile(r"^[-*+]\s+")
_OL = re.compile(r"^\d+[.)]\s+")


def _clean_inline(text: str) -> str:
    text = _IMAGE_INLINE.sub("", text)
    text = _HTML_INLINE.sub("", text)
    text = _LINK.sub(r"\1", text)
    # 强调标记只去符号不留痕：**bold** / *italic* / __x__ / _x_
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    return text.strip()


def markdown_to_plain_text(markdown: str) -> str:
    """
    MinerU markdown → 自然段文本（\\n 分隔，与 docx 提取同构）。

    规则刻意保守：只去标记、跳表格/图片，不改写文字本身——
    批改定位靠原文逐字匹配，归一化多改一个字都会让定位悄悄失效。
    公式（$...$ / $$...$$）按原样保留为文本：第一步不做 LaTeX 语义处理。
    """
    paragraphs: list[str] = []
    in_fence = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not stripped:
            continue
        if in_fence:
            # 代码块内容按原样收（去标记会破坏代码），一行一段
            paragraphs.append(stripped)
            continue
        # 表格行（| 开头）与分隔行（|---|）整行跳过：表格不进批改正文
        if stripped.startswith("|"):
            continue
        if _IMAGE_LINE.match(stripped) or _HTML_TAG_LINE.match(stripped):
            continue
        if _HTML_TABLE_LINE.search(stripped):
            continue
        text = _clean_inline(stripped)
        text = _HEADING.sub("", text)
        text = _UL.sub("", text)
        text = _OL.sub("", text)
        text = text.strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def save_structured(result: MinerUResult, dest_dir: str) -> None:
    """
    把 MinerU 原始产物落盘留存（full.md / content_list.json / images/）。

    目录按 log_id 派生（调用方传 mineru/{log_id}），重跑覆盖写——
    幂等，不需要额外列记录路径。写失败只告警不抛：structured 是旁路留存，
    不该把已经拿到 plain_text 的处理判成失败（同计量旁路的边界）。
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "full.md"), "w", encoding="utf-8") as f:
            f.write(result.markdown)
        if result.content_list is not None:
            with open(os.path.join(dest_dir, "content_list.json"), "w", encoding="utf-8") as f:
                json.dump(result.content_list, f, ensure_ascii=False, indent=2)
        if result.images:
            img_dir = os.path.join(dest_dir, "images")
            os.makedirs(img_dir, exist_ok=True)
            for name, data in result.images.items():
                with open(os.path.join(img_dir, name), "wb") as f:
                    f.write(data)
    except OSError as e:
        logger.warning("MinerU structured 留存失败（不影响批改）：%s", e)
