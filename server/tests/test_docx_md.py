"""本地 docx → Markdown 转换器单测（md 转换任务的 Word 路径）"""

import io

from docx import Document
from docx.oxml.ns import qn

from services.docx_md import docx_to_markdown


def _bytes(doc: Document) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _add_hyperlink(paragraph, url: str, text: str):
    """python-docx 没有公开的超链接 API，按 OOXML 结构手搭"""
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = paragraph._p.makeelement(qn("w:hyperlink"), {qn("r:id"): r_id})
    run = paragraph._p.makeelement(qn("w:r"), {})
    t = paragraph._p.makeelement(qn("w:t"), {})
    t.text = text
    run.append(t)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def test_headings_map_to_hash_levels():
    doc = Document()
    doc.add_heading("一级标题", level=1)
    doc.add_heading("二级标题", level=2)
    doc.add_paragraph("正文段落")
    md = docx_to_markdown(_bytes(doc))
    assert "# 一级标题" in md
    assert "## 二级标题" in md
    assert "正文段落" in md


def test_title_style_becomes_h1():
    doc = Document()
    doc.add_paragraph("文档题目", style="Title")
    assert docx_to_markdown(_bytes(doc)) == "# 文档题目"


def test_chinese_heading_style_recognized():
    doc = Document()
    style = doc.styles.add_style("标题 2", 1)  # 1 = WD_STYLE_TYPE.PARAGRAPH
    doc.add_paragraph("中文样式标题", style=style)
    assert "## 中文样式标题" in docx_to_markdown(_bytes(doc))


def test_bold_italic_runs():
    doc = Document()
    p = doc.add_paragraph()
    p.add_run("加粗").bold = True
    p.add_run("斜体").italic = True
    both = p.add_run("粗斜")
    both.bold = True
    both.italic = True
    p.add_run("普通")
    md = docx_to_markdown(_bytes(doc))
    assert "**加粗**" in md
    assert "*斜体*" in md
    assert "***粗斜***" in md
    assert "普通" in md


def test_hyperlink_text_and_url_kept():
    doc = Document()
    p = doc.add_paragraph("见 ")
    _add_hyperlink(p, "https://example.test", "示例链接")
    md = docx_to_markdown(_bytes(doc))
    assert "[示例链接](https://example.test)" in md


def test_table_becomes_pipe_table():
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "数量"
    table.cell(1, 0).text = "苹果"
    table.cell(1, 1).text = "3"
    md = docx_to_markdown(_bytes(doc))
    assert "| 项目 | 数量 |" in md
    assert "| --- | --- |" in md
    assert "| 苹果 | 3 |" in md


def test_table_cell_pipe_escaped_and_multiline_joined():
    doc = Document()
    table = doc.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    cell.text = "含|竖线"
    cell.add_paragraph("第二段")
    md = docx_to_markdown(_bytes(doc))
    assert "含\\|竖线 第二段" in md


def test_empty_paragraphs_skipped():
    doc = Document()
    doc.add_paragraph("甲")
    doc.add_paragraph("")
    doc.add_paragraph("乙")
    assert docx_to_markdown(_bytes(doc)) == "甲\n\n乙"


def test_empty_document_returns_empty():
    assert docx_to_markdown(_bytes(Document())) == ""


def test_table_and_paragraph_order_preserved():
    doc = Document()
    doc.add_paragraph("表格之前")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "单元格"
    doc.add_paragraph("表格之后")
    md = docx_to_markdown(_bytes(doc))
    assert md.index("表格之前") < md.index("单元格") < md.index("表格之后")
