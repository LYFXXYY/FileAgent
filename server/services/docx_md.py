"""
本地 docx → Markdown 转换 —— md 转换任务的 Word 输入路径

【为什么不走 MinerU】
born-digital Word 的段落/标题/表格结构就在 XML 里，本地转换免费且毫秒级；
MinerU 的比较优势在扫描件与复杂版面，按格式路由才不白花钱（用户拍板）。

【能力边界（v1，有意为之）】
- 覆盖：标题层级、自然段、粗体/斜体、超链接、表格
- 不做：列表编号语义（numPr 段落按普通段落输出）、图片、文本框、脚注
- 正文里的 Markdown 特殊字符不转义：转换目标是可读 md，不是逐字保真容器
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

_HEADING_STYLE = re.compile(r"^(?:Heading|标题)\s*(\d)$")


def _run_to_md(run) -> str:
    text = run.text
    if not text:
        return ""
    if run.bold and run.italic:
        return f"***{text}***"
    if run.bold:
        return f"**{text}**"
    if run.italic:
        return f"*{text}*"
    return text


def _paragraph_to_md(paragraph: Paragraph) -> str:
    parts = []
    # iter_inner_content 按文档顺序给出 Run 与 Hyperlink，
    # 只用 paragraph.text 会丢掉超链接文字
    for child in paragraph.iter_inner_content():
        if hasattr(child, "runs"):  # Hyperlink
            text = "".join(_run_to_md(r) for r in child.runs)
            url = child.address or ""
            parts.append(f"[{text}]({url})" if text and url else text)
        else:
            parts.append(_run_to_md(child))
    text = "".join(parts).strip()
    if not text:
        return ""

    style_name = paragraph.style.name if paragraph.style is not None else ""
    if style_name == "Title":
        return f"# {text}"
    m = _HEADING_STYLE.match(style_name)
    if m:
        level = min(int(m.group(1)), 6)
        return f"{'#' * level} {text}"
    return text


def _table_to_md(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            # 单元格内多段拼成一行；竖线转义、换行转 <br>，否则 md 表格散架
            cell_text = " ".join(p.text for p in cell.paragraphs if p.text.strip())
            cells.append(cell_text.replace("|", "\\|").replace("\n", "<br>"))
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(lines)


def docx_to_markdown(file_bytes: bytes) -> str:
    """把 docx 字节转成 Markdown 文本。空文档返回空串，调用方决定怎么报错"""
    doc = Document(io.BytesIO(file_bytes))
    blocks: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Table):
            md = _table_to_md(block)
        else:
            md = _paragraph_to_md(block)
        if md:
            blocks.append(md)
    return "\n\n".join(blocks)
