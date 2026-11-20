"""OCR 双轨输出层单测 —— markdown 归一化规则 + structured 落盘旁路

归一化是纯函数，逐条钉住规则：批改定位靠原文逐字匹配，
多改一个字都会让 _find_unused_span 悄悄失效。
"""

from __future__ import annotations

import json
import os

from services import ocr
from services.mineru import MinerUResult


# ---------- 行首标记 ----------
def test_headings_and_lists_become_plain_paragraphs():
    md = "# 第一章 总则\n\n## 1.1 范围\n\n- 第一项要求\n* 第二项要求\n1. 有序项\n2) 另一种有序"
    assert ocr.markdown_to_plain_text(md) == (
        "第一章 总则\n1.1 范围\n第一项要求\n第二项要求\n有序项\n另一种有序"
    )


def test_blank_lines_drop_and_paragraphs_join_with_newline():
    md = "第一段。\n\n\n第二段。\n"
    assert ocr.markdown_to_plain_text(md) == "第一段。\n第二段。"


def test_empty_markdown_returns_empty():
    assert ocr.markdown_to_plain_text("") == ""
    assert ocr.markdown_to_plain_text("\n\n  \n") == ""


# ---------- 表格 / 图片 / HTML ----------
def test_table_rows_skipped_entirely():
    """表格第一步不进批改正文（用户拍板）：| 开头的行整行丢"""
    md = "表格前的正文。\n\n| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n\n表格后的正文。"
    assert ocr.markdown_to_plain_text(md) == "表格前的正文。\n表格后的正文。"


def test_image_lines_and_inline_images_removed():
    md = "![图1](images/a.jpg)\n\n正文带图 ![inline](images/b.png) 结束。"
    assert ocr.markdown_to_plain_text(md) == "正文带图  结束。"


def test_html_wrapper_lines_skipped_and_inline_tags_stripped():
    """MinerU 的表格常以 <html><body><table> 包裹，标签不进正文"""
    md = (
        "<html>\n<body>\n<table><tr><td>单元格</td></tr></table>\n</body>\n</html>\n"
        "正文<b>加粗</b>尾。"
    )
    assert ocr.markdown_to_plain_text(md) == "正文加粗尾。"


# ---------- 行内标记 ----------
def test_links_keep_text_drop_url():
    assert ocr.markdown_to_plain_text("见[规范文档](https://example.com/spec.pdf)第 3 条") == (
        "见规范文档第 3 条"
    )


def test_emphasis_markers_removed_without_trace():
    md = "**重点**和*斜体*以及__下划强调__"
    assert ocr.markdown_to_plain_text(md) == "重点和斜体以及下划强调"


def test_formulas_kept_verbatim():
    """第一步不做 LaTeX 语义处理：公式按原样保留为文本"""
    md = "勾股定理 $a^2 + b^2 = c^2$ 成立。"
    assert ocr.markdown_to_plain_text(md) == "勾股定理 $a^2 + b^2 = c^2$ 成立。"


def test_text_itself_never_rewritten():
    """归一化只去标记不改字：错别字必须原样保留，否则批改定位对不上"""
    md = "## 标题\n\n他再接再励地工作，按步就班地推进。"
    assert ocr.markdown_to_plain_text(md) == "标题\n他再接再励地工作，按步就班地推进。"


# ---------- 代码围栏 ----------
def test_fence_content_kept_verbatim_one_line_per_paragraph():
    """围栏内不去了标记：| 和 # 在代码里是内容不是表格/标题"""
    md = "正文前。\n\n```python\n# 注释\n|a|b|\nprint('hi')\n```\n\n正文后。"
    expected = "正文前。\n# 注释\n|a|b|\nprint('hi')\n正文后。"
    assert ocr.markdown_to_plain_text(md) == expected


# ---------- structured 落盘 ----------
def test_save_structured_writes_all_tracks(tmp_path):
    result = MinerUResult(
        markdown="# 标题\n正文",
        content_list=[{"type": "text", "text": "正文", "page_idx": 0}],
        images={"a.jpg": b"\xff\xd8fake"},
    )
    dest = str(tmp_path / "mineru" / "42")
    ocr.save_structured(result, dest)

    assert (tmp_path / "mineru" / "42" / "full.md").read_text(encoding="utf-8") == "# 标题\n正文"
    saved = json.loads(
        (tmp_path / "mineru" / "42" / "content_list.json").read_text(encoding="utf-8")
    )
    assert saved == [{"type": "text", "text": "正文", "page_idx": 0}]
    assert (tmp_path / "mineru" / "42" / "images" / "a.jpg").read_bytes() == b"\xff\xd8fake"


def test_save_structured_overwrites_on_rerun(tmp_path):
    """重跑覆盖写（幂等）：第二次落盘不留上一轮的旧文件内容"""
    dest = str(tmp_path / "m")
    ocr.save_structured(MinerUResult(markdown="旧"), dest)
    ocr.save_structured(MinerUResult(markdown="新"), dest)
    assert (tmp_path / "m" / "full.md").read_text(encoding="utf-8") == "新"


def test_save_structured_skips_absent_tracks(tmp_path):
    dest = str(tmp_path / "m")
    ocr.save_structured(MinerUResult(markdown="只有 md"), dest)
    assert os.listdir(dest) == ["full.md"]


def test_save_structured_oserror_is_side_path(monkeypatch, tmp_path):
    """写盘失败只告警不抛：structured 是旁路，不能把已拿到正文的处理判成失败"""

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(ocr.os, "makedirs", _boom)
    ocr.save_structured(MinerUResult(markdown="x"), str(tmp_path / "m"))  # 不抛即通过
