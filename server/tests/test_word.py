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


# ---------- 依据条款页（阶段 6 A7） ----------
def _all_text(doc) -> str:
    return "\n".join(p.text for p in doc.paragraphs)


def test_diff_docx_without_refs_has_no_refs_page():
    """没召回条款时交付件与阶段 6 之前完全一致——不多一页也不多一行"""
    buf = create_diff_docx("原文一\n原文二", "修订一\n原文二")
    text = _all_text(Document(buf))

    assert "本次批改依据的规范条款" not in text
    assert "修订后完整文本（可直接复制）" in text


def test_diff_docx_appends_refs_page():
    refs = [
        {"clause": "1.1.1", "excerpt": "1.1.1 同音别字", "score": 0.5, "via": "lexical"},
        {"clause": "2.1.1", "excerpt": "2.1.1 的字误用", "score": 0.4, "via": "vector"},
    ]
    buf = create_diff_docx("他高兴的跳了起来。", "他高兴地跳了起来。", spec_refs=refs)
    doc = Document(buf)
    text = _all_text(doc)

    assert "本次批改依据的规范条款" in text
    assert "1.1.1 同音别字" in text
    assert "2.1.1 的字误用" in text
    # 条款页排在"修订后完整文本"之后：先看结果，再看依据
    assert text.index("修订后完整文本") < text.index("本次批改依据的规范条款")


def test_refs_page_does_not_dump_clause_bodies():
    """
    交付件会被外发，只列条款号与标题行，不整段转载规范正文。

    抄进正文等于替用户做了一次对外转载，而且让批改稿长得没法看。
    """
    refs = [
        {
            "clause": "1.1.1",
            "excerpt": "1.1.1 同音别字\n【判定标准】替换后语义不通即判定为错别字。",
            "score": 0.5,
            "via": "lexical",
        }
    ]
    text = _all_text(Document(create_diff_docx("甲", "乙", spec_refs=refs)))

    assert "1.1.1 同音别字" in text
    assert "【判定标准】" not in text


def test_refs_page_survives_empty_excerpt():
    """引用缺 excerpt 也不能炸——历史数据或解析失败时就是这个形状"""
    buf = create_diff_docx("甲", "乙", spec_refs=[{"clause": "9.9.9", "score": 0.1}])
    assert "9.9.9" in _all_text(Document(buf))
