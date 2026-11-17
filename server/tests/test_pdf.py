"""
PDF 提取单测 —— 段落拼接、噪声剔除、错误路径

纯函数 + 内存里的合成 PDF（pymupdf 自己造），不连 Postgres、不出网。
"""

import pymupdf
import pytest

from services.pdf import (
    PdfError,
    _is_page_number,
    _join_block_lines,
    _merge_blocks,
    _noise_texts,
    extract_text_from_pdf,
)

# ---------- 造 PDF 的小工具 ----------


def _pdf(*pages, cjk=False, repeat_header=None, page_numbers=True):
    """
    每个位置参数是一页的视觉行列表；空串表示额外段间距

    行内容必须逐页不同（除了故意重复的页眉）——真实文档不会每页吐一样的正文，
    写成一样会被跨页高频判据当装饰删掉，测试就成了在测自己的夹具。
    幻灯导出的件就是"每行各占一个 block"，这样造出来的夹具正好走最难的那条路径。
    """
    doc = pymupdf.open()
    kw = {"fontname": "china-s", "fontsize": 11} if cjk else {"fontsize": 11}
    for i, lines in enumerate(pages):
        page = doc.new_page()
        y = 72.0
        if repeat_header:
            page.insert_text((72, 50), repeat_header, fontsize=9)
        for text in lines:
            if text:
                page.insert_text((72, y), text, **kw)
            y += 17.0 if text else 34.0
        if page_numbers:
            page.insert_text((72, 700), str(i + 1), fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def _para(text, left=72.0, top=60.0, bottom=75.0):
    """_merge_blocks / _noise_texts 的入参形状：(文本, 左边缘, 上边, 下边)"""
    return (text, left, top, bottom)


# ---------- block 内按行拼接 ----------
def test_join_cjk_lines_without_space():
    """中文换行本来就不留空格，补了空格反而让原文匹配不上"""
    assert _join_block_lines("第一段内容\n接着同一行后面") == "第一段内容接着同一行后面"


def test_join_latin_lines_with_space():
    assert _join_block_lines("hello\nworld") == "hello world"


def test_join_rejoins_hyphenated_english_word():
    assert _join_block_lines("under-\nstanding") == "understanding"


def test_join_drops_blank_lines_and_returns_empty_for_nothing():
    assert _join_block_lines("第一段\n\n\n第二段\n") == "第一段第二段"
    assert _join_block_lines("\n  \n") == ""


# ---------- 几何邻接合并 ----------
def test_merge_adjacent_same_column_blocks():
    """每行一个 block 的幻灯导出件：紧邻同栏的碎行必须接回一段"""
    blocks = [_para("This is a long", bottom=75.0), _para("three visual lines", top=77.0)]
    assert _merge_blocks(blocks) == ["This is a long three visual lines"]


def test_merge_stops_at_sentence_end():
    merged = _merge_blocks([_para("第一段说完了。", bottom=75.0), _para("第二段开头", top=77.0)])
    assert merged == ["第一段说完了。", "第二段开头"]


def test_merge_never_crosses_columns():
    """双栏排版里左右栏垂直上可能"紧邻"，靠左边缘差值挡住，不能缝成一段"""
    merged = _merge_blocks(
        [_para("左栏结尾", left=72.0, bottom=75.0), _para("右栏开头", left=320.0, top=77.0)]
    )
    assert merged == ["左栏结尾", "右栏开头"]


def test_merge_respects_paragraph_gap():
    merged = _merge_blocks([_para("上一段", bottom=75.0), _para("空一行后的新段", top=120.0)])
    assert merged == ["上一段", "空一行后的新段"]


def test_merge_empty_page():
    assert _merge_blocks([]) == []


# ---------- 页码 / 装饰噪声 ----------
def test_page_number_only_in_margin_band():
    """落在页脚带里才算页码；版心中的四位数字（年份、序号）必须留下"""
    assert _is_page_number("12", 700.0, 842.0) is True
    assert _is_page_number("2024", 300.0, 842.0) is False
    assert _is_page_number("IX", 40.0, 842.0) is True
    assert _is_page_number("第 3 节", 700.0, 842.0) is False


def test_noise_needs_three_pages():
    """短文档不启用高频判据，免得把真正重复两次的句子当模板删掉"""
    pages = [[_para("每页都一样")], [_para("每页都一样")]]
    assert _noise_texts(pages) == set()


def test_noise_detects_repeated_running_header():
    pages = [[_para("XX大学实验报告")] for _ in range(5)]
    assert _noise_texts(pages) == {"XX大学实验报告"}


def test_noise_spares_repeated_sentences():
    """带句末标点的重复行是内容（诗词副歌、免责声明），不能当装饰删"""
    pages = [[_para("春天来了，花开了。")] for _ in range(5)]
    assert _noise_texts(pages) == set()


def test_noise_spares_long_repeated_blocks():
    pages = [[_para("很长的一段重复文本" * 4)] for _ in range(5)]
    assert _noise_texts(pages) == set()


# ---------- 错误路径 ----------
def test_extract_rejects_non_pdf_bytes():
    with pytest.raises(PdfError, match="PDF 打开失败"):
        extract_text_from_pdf(b"definitely not a pdf")


def test_extract_rejects_encrypted_pdf():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret body text")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    with pytest.raises(PdfError, match="密码"):
        extract_text_from_pdf(data)


def test_extract_rejects_pdf_without_text_layer():
    """扫描件必须报错，不能回空文本让用户以为文档本来就是空的"""
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    with pytest.raises(PdfError, match="文字层"):
        extract_text_from_pdf(data)


# ---------- 端到端（合成 PDF） ----------
def test_extract_merges_per_line_blocks_into_paragraphs():
    text = extract_text_from_pdf(
        _pdf(["This is a long", "sentence split over", "lines", "", "New paragraph."])
    )
    assert text == "This is a long sentence split over lines\nNew paragraph."


def test_extract_drops_page_numbers_and_running_header():
    text = extract_text_from_pdf(
        _pdf(
            ["正文甲第一行", "正文甲第二行"],
            ["正文乙第一行", "正文乙第二行"],
            ["正文丙第一行", "正文丙第二行"],
            cjk=True,
            repeat_header="XX大学计算机学院实验报告",
        )
    )
    assert text == "正文甲第一行正文甲第二行\n正文乙第一行正文乙第二行\n正文丙第一行正文丙第二行"


def test_extract_chinese_lines_join_into_paragraphs():
    text = extract_text_from_pdf(
        _pdf(["这是第一段的全部内容，", "它在版面上被切成三行。", "", "这是第二段。"], cjk=True)
    )
    assert text == "这是第一段的全部内容，它在版面上被切成三行。\n这是第二段。"
