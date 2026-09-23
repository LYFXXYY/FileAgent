"""word 服务单测 —— Markdown 清理、文本提取、docx 生成"""

from io import BytesIO

from docx import Document

from services.word import (
    create_diff_docx,
    create_plain_docx,
    extract_text_from_docx,
    strip_markdown,
)


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_strip_markdown_inline():
    assert strip_markdown("**加粗**") == "加粗"
    assert strip_markdown("*斜体*") == "斜体"
    assert strip_markdown("~~删除~~") == "删除"
    assert strip_markdown("`code`") == "code"


def test_strip_markdown_headings_and_lists():
    assert strip_markdown("# 标题") == "标题"
    assert strip_markdown("### 三级") == "三级"
    assert strip_markdown("- 项目") == "项目"
    assert strip_markdown("* 项目") == "项目"
    assert strip_markdown("1. 第一") == "第一"


def test_extract_text_joins_paragraphs_with_newline():
    raw = _docx_bytes(["第一段", "第二段"])
    assert extract_text_from_docx(raw) == "第一段\n第二段"


def test_create_plain_docx_returns_readable_docx():
    buf = create_plain_docx("**你好**\n世界")
    doc = Document(buf)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "你好" in text and "**" not in text
    assert "世界" in text


def test_create_diff_docx_returns_valid_docx():
    buf = create_diff_docx("这是我的书", "这是你的书")
    doc = Document(buf)
    # 应能正常解析，且包含修订后完整文本区块
    full = "\n".join(p.text for p in doc.paragraphs)
    assert "这是你的书" in full
    assert "修订后完整文本" in full


def test_create_diff_docx_marks_changes_red():
    from docx.shared import RGBColor

    buf = create_diff_docx("原文内容", "原文改动")
    doc = Document(buf)
    red = RGBColor(0xFF, 0x00, 0x00)
    found_red = False
    for para in doc.paragraphs:
        for run in para.runs:
            if run.font.color and run.font.color.rgb == red:
                found_red = True
    assert found_red, "diff 文档应包含红色标注的改动"
