"""块驱动 PPT 渲染器单测 —— 合成块夹具匹配真实 MinerU 形状，不出网。

用 python-pptx 重读产物验证：可编辑文本框 / 原生可编辑表 / 图片 / 噪声跳过，
以及 B 流式重排的完整性不变式（切页不丢字、超高原生表按行拆、密集页兜底 A）。
"""

from __future__ import annotations

import io

import pymupdf
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Pt

from services.ppt_convert import parse_html_table, render_blocks_to_pptx

_PNG = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4)).tobytes("png")
_HTML = (
    "<table><tr><td rowspan=1 colspan=1>软件包</td><td rowspan=1 colspan=1>用途</td></tr>"
    "<tr><td rowspan=1 colspan=1>vim</td><td rowspan=1 colspan=1>编辑器</td></tr></table>"
)


def _title(text, level=2, bbox=(100, 100, 500, 140)):
    return {
        "type": "title",
        "content": {"title_content": [{"type": "text", "content": text}], "level": level},
        "bbox": list(bbox),
    }


def _para(text, bbox=(100, 150, 500, 200)):
    return {
        "type": "paragraph",
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
        "bbox": list(bbox),
    }


def _image(path="images/a.jpg", bbox=(100, 210, 300, 400)):
    return {
        "type": "image",
        "content": {"image_source": {"path": path}, "image_caption": [], "image_footnote": []},
        "bbox": list(bbox),
    }


def _table(html=_HTML, path="images/t.jpg", bbox=(100, 410, 500, 600)):
    content = {
        "image_source": {"path": path},
        "table_caption": [],
        "table_footnote": [],
        "table_type": "complex_table",
        "table_nest_level": 1,
    }
    if html is not None:
        content["html"] = html
    return {"type": "table", "content": content, "bbox": list(bbox)}


def _read(data: bytes) -> Presentation:
    return Presentation(io.BytesIO(data))


def _texts(prs) -> list[str]:
    out = []
    for slide in prs.slides:
        out.extend(_slide_texts(slide))
    return out


def _slide_texts(slide) -> list[str]:
    return [s.text_frame.text for s in slide.shapes if s.has_text_frame]


# ---------- html 表格解析 ----------
def test_parse_html_table_simple_grid():
    n_rows, n_cols, anchors, spans = parse_html_table(_HTML)
    assert (n_rows, n_cols) == (2, 2)
    assert anchors[(0, 0)] == "软件包"
    assert anchors[(1, 1)] == "编辑器"
    assert spans == []


def test_parse_html_table_expands_spans():
    html = "<table><tr><td rowspan=2 colspan=2>合并</td><td>x</td></tr><tr><td>y</td></tr></table>"
    n_rows, n_cols, anchors, spans = parse_html_table(html)
    assert (n_rows, n_cols) == (2, 3)
    assert anchors[(0, 0)] == "合并"
    assert anchors[(0, 2)] == "x"
    assert anchors[(1, 2)] == "y"
    assert (0, 0, 2, 2) in spans


def test_parse_html_table_empty_returns_none():
    assert parse_html_table("") is None
    assert parse_html_table("<table></table>") is None


# ---------- 渲染 ----------
def test_render_title_and_paragraph_editable():
    data = render_blocks_to_pptx([[_title("标题"), _para("正文段落")]], {})
    prs = _read(data)
    assert len(list(prs.slides)) == 1
    texts = _texts(prs)
    assert "标题" in texts
    assert "正文段落" in texts


def test_render_native_editable_table():
    data = render_blocks_to_pptx([[_table()]], {"t.jpg": _PNG})
    prs = _read(data)
    tables = [s.table for s in prs.slides[0].shapes if s.has_table]
    assert len(tables) == 1
    assert tables[0].cell(0, 0).text == "软件包"
    assert tables[0].cell(1, 0).text == "vim"
    assert len(tables[0].rows) == 2
    assert len(tables[0].columns) == 2


def test_render_image_placed():
    data = render_blocks_to_pptx([[_image()]], {"a.jpg": _PNG})
    prs = _read(data)
    pics = [s for s in prs.slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert len(pics) == 1


def test_render_table_fallback_to_image_when_no_html():
    data = render_blocks_to_pptx([[_table(html=None)]], {"t.jpg": _PNG})
    prs = _read(data)
    shapes = prs.slides[0].shapes
    assert not any(s.has_table for s in shapes)
    assert any(s.shape_type == MSO_SHAPE_TYPE.PICTURE for s in shapes)


def test_render_missing_image_placeholder():
    data = render_blocks_to_pptx([[_image(path="images/none.jpg")]], {})
    assert "（图片缺失）" in _texts(_read(data))


def test_font_fits_small_title_box():
    # 小框标题按 bbox 反推字号：A4 适配后框 76.3×21.6pt，4 字单行 → 18pt
    # （写死 22pt 会换行成两行并超出框高，正是真实文档实拍的压盖缺陷）
    data = render_blocks_to_pptx([[_title("定义核心", bbox=(100, 100, 300, 140))]], {}, mode="page")
    prs = _read(data)
    tb = next(s for s in prs.slides[0].shapes if s.has_text_frame)
    assert tb.text_frame.text == "定义核心"
    assert tb.text_frame.paragraphs[0].runs[0].font.size == Pt(18)


def test_landscape_keeps_header_footer():
    blocks = [
        {
            "type": "page_header",
            "content": {"page_header_content": [{"type": "text", "content": "页标题"}]},
            "bbox": [0, 0, 400, 60],
        },
        {
            "type": "page_footer",
            "content": {"page_footer_content": [{"type": "text", "content": "品牌标"}]},
            "bbox": [0, 940, 400, 980],
        },
        {
            "type": "page_number",
            "content": {"page_number_content": [{"type": "text", "content": "4"}]},
            "bbox": [480, 940, 500, 980],
        },
        _para("正文"),
    ]
    texts = _texts(_read(render_blocks_to_pptx([blocks], {}, [(842, 474)])))
    assert "页标题" in texts
    assert "品牌标" in texts
    assert "正文" in texts
    assert "4" not in texts


def test_noise_blocks_skipped():
    blocks = [
        {
            "type": "page_header",
            "content": {"page_header_content": [{"type": "text", "content": "页眉"}]},
            "bbox": [0, 0, 100, 20],
        },
        {
            "type": "page_footer",
            "content": {"page_footer_content": [{"type": "text", "content": "页脚"}]},
            "bbox": [0, 800, 100, 820],
        },
        {
            "type": "page_number",
            "content": {"page_number_content": [{"type": "text", "content": "4"}]},
            "bbox": [280, 800, 300, 820],
        },
        _para("唯一正文"),
    ]
    texts = _texts(_read(render_blocks_to_pptx([blocks], {})))
    assert texts == ["唯一正文"]


def test_slide_count_matches_pages():
    data = render_blocks_to_pptx([[_para("第一页")], [_para("第二页")]], {}, mode="page")
    assert len(list(_read(data).slides)) == 2


def test_empty_pages_placeholder():
    prs = _read(render_blocks_to_pptx([], {}))
    assert len(list(prs.slides)) == 1
    assert "（无内容）" in _texts(prs)


def test_page_sizes_scale_bbox():
    # 不同页尺寸不应抛；产物仍是合法 pptx
    data = render_blocks_to_pptx([[_para("a")], [_para("b")]], {}, [(595, 842), (842, 595)])
    assert len(list(_read(data).slides)) == 2


# ---------- B 流式重排（auto 默认走流，横向/密集页兜底 A） ----------
def test_flow_packs_short_pages_into_one_slide():
    data = render_blocks_to_pptx([[_para("第一页短文")], [_para("第二页短文")]], {})
    prs = _read(data)
    assert len(list(prs.slides)) == 1
    texts = _texts(prs)
    assert "第一页短文" in texts and "第二页短文" in texts


def test_flow_level1_title_starts_new_slide():
    blocks = [_para("前言"), _title("第一章", level=1), _para("章内容")]
    prs = _read(render_blocks_to_pptx([blocks], {}))
    slides = list(prs.slides)
    assert len(slides) == 2
    assert "前言" in _slide_texts(slides[0])
    assert "第一章" in _slide_texts(slides[1]) and "章内容" in _slide_texts(slides[1])


def test_flow_heading_orphan_guard_moves_to_next_slide():
    # 600 字 ≈ 17 行 × 27pt = 459pt，几乎占满 468pt 内容高；
    # 节标题带"两行正文"护栏装不下，必须另起一页而不是独悬页底
    blocks = [_para("字" * 600), _title("节标题", level=2)]
    prs = _read(render_blocks_to_pptx([blocks], {}))
    slides = list(prs.slides)
    assert len(slides) == 2
    assert "节标题" in _slide_texts(slides[1])
    assert "节标题" not in _slide_texts(slides[0])


def test_flow_long_paragraph_split_keeps_every_char():
    long_text = "护" * 800
    prs = _read(render_blocks_to_pptx([[_para(long_text)]], {}))
    assert len(list(prs.slides)) >= 2
    # 切片是原文连续子串：按形状顺序拼回必须逐字相等（不丢字、不插字）
    assert "".join(_texts(prs)) == long_text


def test_flow_dense_page_falls_back_to_bbox_position():
    # 视觉块归一化面积 0.5 ≥ 0.45 → 密集页兜底 A：图按 bbox 落位而非流式居中
    dense = [_image(bbox=(0, 0, 1000, 500))]
    prs = _read(
        render_blocks_to_pptx([dense, [_para("后续流式页")]], {"a.jpg": _PNG}, [(595, 842)] * 2)
    )
    slides = list(prs.slides)
    assert len(slides) == 2
    pic = next(s for s in slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE)
    # A4 纵向适配：ph=540pt、pw≈381.6pt、ox≈169.2pt；bbox 上半页 → top=0
    assert pic.top == 0
    assert abs(pic.left / 12700 - 169.2) < 1.0
    assert abs(pic.height / 12700 - 270.0) < 1.0
    # 兜底页插断流后，下一页流式内容另起页且回到流式边距
    tb = next(s for s in slides[1].shapes if s.has_text_frame)
    assert tb.text_frame.text == "后续流式页"
    assert tb.left == 40 * 12700


def test_flow_mode_forced_on_landscape_skips_header():
    blocks = [
        {
            "type": "page_header",
            "content": {"page_header_content": [{"type": "text", "content": "页标题"}]},
            "bbox": [0, 0, 400, 60],
        },
        _para("正文"),
    ]
    prs = _read(render_blocks_to_pptx([blocks], {}, [(842, 474)], mode="flow"))
    assert len(list(prs.slides)) == 1
    assert _texts(prs) == ["正文"]


def _rows_html(n_rows: int, *, span: bool = False) -> str:
    rows = []
    for i in range(n_rows):
        rs = n_rows if (span and i == 0) else 1
        rows.append(f"<tr><td rowspan={rs} colspan=1>格{i}</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def test_flow_tall_table_row_chunked_keeps_all_cells():
    # 40 行 × 22.4pt = 896pt > 468pt 内容高，无跨格 → 按行拆成多张原生表
    data = render_blocks_to_pptx([[_table(html=_rows_html(40))]], {"t.jpg": _PNG})
    prs = _read(data)
    slides = list(prs.slides)
    assert len(slides) == 2
    seen = []
    for slide in slides:
        tables = [s.table for s in slide.shapes if s.has_table]
        assert len(tables) == 1
        seen.extend(c.text for row in tables[0].rows for c in row.cells)
    assert seen == [f"格{i}" for i in range(40)]


def test_flow_tall_table_with_spans_falls_back_to_image():
    # 跨格不能跨页拆：超高 + 带 rowspan → 回落渲染图
    data = render_blocks_to_pptx([[_table(html=_rows_html(30, span=True))]], {"t.jpg": _PNG})
    prs = _read(data)
    shapes = prs.slides[0].shapes
    assert not any(s.has_table for s in shapes)
    assert any(s.shape_type == MSO_SHAPE_TYPE.PICTURE for s in shapes)
