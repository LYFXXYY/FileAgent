"""
块驱动 PPT 忠实转换渲染器（feature-2 片1）

【与阶段5大纲驱动的区别】
阶段5 `pptx_builder.build_pptx(outline)` 是"LLM 把正文重组成大纲 bullets"，
会丢图丢表、且改了原文结构——那是"修改"不是"转换"。本模块反过来：
直接消费 MinerU 的 content_list 块（title/paragraph/image/table），
**忠实搬运**到幻灯片，文字进可编辑文本框、表格进原生可编辑 PPT 表、图按原位贴。

【输入形状】（2026-11-21 真实文档探明，见 SPEC §9.17）
- pages: content_list，页嵌套——外层页列表、内层块列表
- images: MinerUResult.images，键是 basename（{sha256}.jpg）
- page_sizes: 每页 (宽pt, 高pt)，取自 MinerUResult.page_sizes（layout.json）；
  缺省退 A4。content_list 的 bbox 归一化到 1000×1000，按页面宽高比适配进幻灯片。

【布局双模：B 阅读顺序重排为主、A 整页忠实兜底】
- A（page）：每页一张幻灯片、块按 bbox 定位，保证完整性与原位。
- B（flow）：纵向文档页的块按阅读顺序流式排入幻灯片——正文固定可读字号、
  一级标题另起一页、二三级标题作 inline 节标题、图/表按流插放，超长段落按行
  切页（切页不丢字）。横向页（幻灯导出件）与密集页（视觉块面积占比 ≥45% 或
  块数 >30）回落 A：前者本来就是幻灯片，重排反而毁设计；后者重排会挤爆流式版面。
- mode="auto"（默认）按上述规则逐页分流；"page"/"flow" 强制单模（flow 仍对密集页兜底）。
表格 html 解析失败回落渲染图。
字号不写死：按 bbox 几何反推"换行后仍装得进框"的最大字号（原页字号就藏在
bbox 高度里），框内零内边距 + 显式行距——bbox 互不重叠，字装进框即不互相压盖。
噪声按页面朝向区分：横向页（幻灯导出件）的页眉就是页标题、页脚是品牌标，
保留；纵向页（文档件）的页眉页脚页码才是运行噪声，跳过。

【Java 对比】
类似一个无状态 Converter：入参是 DTO（块列表），出参是字节流，
不碰 DB、不发 HTTP（同 services/llm.py 传输层边界），便于纯函数单测。
"""

from __future__ import annotations

import io
import math
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

import pymupdf
from pptx import Presentation
from pptx.util import Pt

# 页码任何朝向都是噪声；页眉/页脚只在纵向文档页算噪声（见模块 docstring）
_ALWAYS_NOISE = {"page_number"}
_PORTRAIT_NOISE = {"page_header", "page_footer"}
# 缺页尺寸时的兜底（A4 纵向，单位 pt）
_DEFAULT_PAGE_PT = (595.0, 842.0)
# 显式绝对行距：拟合字号与真实渲染用同一系数，估高才等于实高。
# 倍数行距（line_spacing=1.25）会被 PowerPoint 再乘默认单行高，估不准
_LINE_PITCH_SINGLE = 1.2
_LINE_PITCH_MULTI = 1.5
_MIN_FONT_PT = 6.0
_EMU_PER_PT = 12700
# B 流式版面：固定可读字号（不再按 bbox 反推），页边距与项间距
_FLOW_MARGIN_PT = 40.0
_FLOW_TOP_PT = 36.0
_FLOW_BODY_PT = 18.0
_FLOW_HEAD_PT = 22.0
_FLOW_TITLE_PT = 32.0
_FLOW_GAP_PT = 10.0
_FLOW_TABLE_PT = 14.0
# 流式图片高度上限（占内容高比例）：整高贴图和真实文档实拍会把图文拆成
# 56 页碎片（一张图独占一页、几字小节悬在上一页）；限高让图文能同屏
_FLOW_IMAGE_MAXH = 0.6
# 低分辨率装饰图（logo）按版面限高放大到 2 倍以上就糊边（2026-11-21 真实文档实拍）；
# 1px 自然显示 0.75pt，2 倍放大上限 → 每像素最多 1.5pt
_FLOW_IMAGE_MAX_UPSCALE_EMU = int(1.5 * _EMU_PER_PT)
# 密集页判据：视觉块（图/表/chart）归一化面积占比或块数超阈即回落 A
_DENSE_VISUAL_AREA = 0.45
_DENSE_BLOCK_COUNT = 30
_VISUAL_KINDS = {"image", "table", "chart"}


class PptConvertError(Exception):
    """块渲染失败统一出口。调用方接住转 _mark_failed"""


@dataclass
class _Cell:
    text: str
    rowspan: int
    colspan: int


class _TableParser(HTMLParser):
    """把 MinerU 的 <table><tr><td rowspan colspan> 解析成行/单元格列表"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._buf: list[str] | None = None
        self._rs = 1
        self._cs = 1

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._buf = []
            self._rs = _to_int(a.get("rowspan"), 1)
            self._cs = _to_int(a.get("colspan"), 1)

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._buf is not None:
            self._row.append(_Cell("".join(self._buf).strip(), self._rs, self._cs))
            self._buf = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _to_int(val, default: int) -> int:
    try:
        n = int(val)
        return n if n >= 1 else default
    except (TypeError, ValueError):
        return default


def parse_html_table(html: str):
    """解析 html 表格 → (行数, 列数, 锚点格{(r,c):text}, 跨格[(r,c,rs,cs)])。

    用占位集展开 rowspan/colspan 成规整网格。无行无列返回 None（调用方回落图）。
    """
    parser = _TableParser()
    parser.feed(html)
    rows = parser.rows
    if not rows:
        return None
    occupied: set[tuple[int, int]] = set()
    anchors: dict[tuple[int, int], str] = {}
    spans: list[tuple[int, int, int, int]] = []
    n_cols = 0
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in occupied:
                c += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied.add((r + dr, c + dc))
            anchors[(r, c)] = cell.text
            if cell.rowspan > 1 or cell.colspan > 1:
                spans.append((r, c, cell.rowspan, cell.colspan))
            c += cell.colspan
            n_cols = max(n_cols, c)
    if n_cols == 0:
        return None
    return len(rows), n_cols, anchors, spans


def _block_text(block: dict) -> str:
    """从块的 *_content 列表里拼出纯文本（title/paragraph 通用）"""
    content = block.get("content") or {}
    parts: list[str] = []
    for value in content.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("content", ""))
    return "".join(parts)


def _bbox_to_emu(bbox, page_w, page_h, slide_w, slide_h):
    """content_list bbox（归一化 0..1000）→ 幻灯片 EMU (left,top,width,height)。

    MinerU 把每页 bbox 独立归一化到 1000×1000（x/页宽*1000、y/页高*1000），
    所以不能当 pt 用。先按真实页面宽高比把整页适配进幻灯片并居中，
    再按归一化比例落块——这样既保页面 aspect 又忠实还原块位置。
    """
    x0, y0, x1, y1 = bbox
    page_aspect = page_w / page_h
    if slide_w / slide_h > page_aspect:
        ph = slide_h
        pw = slide_h * page_aspect
    else:
        pw = slide_w
        ph = slide_w / page_aspect
    ox = (slide_w - pw) / 2
    oy = (slide_h - ph) / 2
    left = int(ox + x0 / 1000 * pw)
    top = int(oy + y0 / 1000 * ph)
    width = max(int((x1 - x0) / 1000 * pw), 1)
    height = max(int((y1 - y0) / 1000 * ph), 1)
    return left, top, width, height


def _eff_len(text: str) -> float:
    """CJK 全角按 1 字宽、其余按半角 0.5 估算字符宽度（单位=字号）"""
    return sum(1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.5 for ch in text)


def _fit_font_pt(text: str, width_pt: float, height_pt: float, max_pt: float) -> tuple[float, int]:
    """取"换行后仍装进 bbox"的最大字号，返回 (字号, 行数)。原页字号藏在 bbox
    高度里，写死字号会把小框标题挤成竖排并压盖邻块（2026-11-21 真实文档实拍）。"""
    if not text:
        return max_pt, 1
    width_pt = max(width_pt, 1.0)
    segments = text.split("\n")
    font = max_pt
    while font > _MIN_FONT_PT:
        lines = sum(max(1, math.ceil(_eff_len(s) * font / width_pt)) for s in segments)
        pitch = _LINE_PITCH_SINGLE if lines == 1 else _LINE_PITCH_MULTI
        if lines * font * pitch <= height_pt:
            return font, lines
        font -= 0.5
    return _MIN_FONT_PT, len(segments)


def _add_textbox(slide, text, box, *, max_pt, bold):
    left, top, width, height = box
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    # 零内边距：默认 0.1in 边距会吃掉小框近一成宽度，拟合就失真了
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.text = text or ""
    font_pt, lines = _fit_font_pt(text or "", width / _EMU_PER_PT, height / _EMU_PER_PT, max_pt)
    pitch = _LINE_PITCH_SINGLE if lines == 1 else _LINE_PITCH_MULTI
    for para in tf.paragraphs:
        para.line_spacing = Pt(font_pt * pitch)
        para.space_before = Pt(0)
        para.space_after = Pt(0)
        for run in para.runs:
            run.font.size = Pt(font_pt)
            run.font.bold = bold


def _add_image(slide, images, path, box) -> bool:
    key = path.rsplit("/", 1)[-1]
    blob = images.get(key)
    if blob is None:
        return False
    left, top, width, height = box
    slide.shapes.add_picture(io.BytesIO(blob), left, top, width=width, height=height)
    return True


def _place_native_table(slide, parsed, box, font):
    """把解析好的网格放进原生可编辑表（A/B 共用），字号由调用方定"""
    n_rows, n_cols, anchors, spans = parsed
    left, top, width, height = box
    table = slide.shapes.add_table(n_rows, n_cols, left, top, width, height).table
    for (r, c), text in anchors.items():
        table.cell(r, c).text = text
    for r, c, rs, cs in spans:
        table.cell(r, c).merge(table.cell(r + rs - 1, c + cs - 1))
        table.cell(r, c).text = anchors.get((r, c), "")
    for row in table.rows:
        for cell in row.cells:
            cell.margin_left = cell.margin_right = Pt(2)
            cell.margin_top = cell.margin_bottom = Pt(1)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(font)


def _add_table(slide, content, images, box):
    """表格优先解析 html 成原生可编辑表；失败回落渲染图，再失败占位文本"""
    html = content.get("html")
    parsed = parse_html_table(html) if html else None
    if parsed is None:
        path = (content.get("image_source") or {}).get("path")
        if path and _add_image(slide, images, path, box):
            return
        _add_textbox(slide, "（表格解析失败）", box, max_pt=12, bold=False)
        return
    # 单元格字号按行高反推，避免默认 18pt 撑破小格
    font = min(14.0, max(_MIN_FONT_PT, box[3] / _EMU_PER_PT / parsed[0] / 1.6))
    _place_native_table(slide, parsed, box, font)


def _render_block(slide, block, images, box, font_scale: float = 1.0):
    kind = block.get("type")
    content = block.get("content") or {}
    if kind in _VISUAL_KINDS and kind != "table":
        path = (content.get("image_source") or {}).get("path")
        if not (path and _add_image(slide, images, path, box)):
            _add_textbox(slide, "（图片缺失）", box, max_pt=12, bold=False)
    elif kind == "table":
        _add_table(slide, content, images, box)
    elif kind == "title":
        max_pt = {1: 40, 2: 32}.get(content.get("level", 2), 24) * font_scale
        _add_textbox(slide, _block_text(block), box, max_pt=max_pt, bold=True)
    else:
        _add_textbox(slide, _block_text(block), box, max_pt=24 * font_scale, bold=False)


def _count_lines(text: str, font: float, width_pt: float) -> int:
    return sum(max(1, math.ceil(_eff_len(s) * font / width_pt)) for s in text.split("\n"))


def _split_text_pieces(text: str, font: float, width_pt: float, height_pt: float) -> list[str]:
    """把超长段落按"整页装得下的行数"切成连续子串（切页不丢字、不插字）。

    贪心按有效字宽走字符：满一行换行、遇 \\n 强制换行；行数超预算就在当前行
    起点切一刀。返回的每片都是原文的连续切片，拼起来与原文逐字相等。
    """
    max_lines = max(1, int(height_pt // (font * _LINE_PITCH_MULTI)))
    if _count_lines(text, font, width_pt) <= max_lines:
        return [text]
    budget = width_pt / font
    pieces: list[str] = []
    cur_start = 0
    line_eff = 0.0
    lines = 1
    for i, ch in enumerate(text):
        if ch == "\n":
            if lines >= max_lines:
                pieces.append(text[cur_start : i + 1])
                cur_start = i + 1
                lines = 1
            else:
                lines += 1
            line_eff = 0.0
            continue
        w = 1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.5
        if line_eff + w > budget:
            if lines >= max_lines:
                pieces.append(text[cur_start:i])
                cur_start = i
                lines = 1
            else:
                lines += 1
            line_eff = 0.0
        line_eff += w
    pieces.append(text[cur_start:])
    return [p for p in pieces if p]


def _image_native_size(blob: bytes) -> tuple[int, int] | None:
    try:
        pix = pymupdf.Pixmap(io.BytesIO(blob))
        return pix.width, pix.height
    except Exception:
        return None


class _FlowDeck:
    """B 流式版面状态机：游标 y 往下走，装不下就换页。只摆位置不改文字。"""

    def __init__(self, prs: Presentation, font_scale: float = 1.0) -> None:
        self.prs = prs
        self.blank = prs.slide_layouts[6]
        self.margin = int(_FLOW_MARGIN_PT * _EMU_PER_PT)
        self.top = int(_FLOW_TOP_PT * _EMU_PER_PT)
        self.content_w = prs.slide_width - 2 * self.margin
        self.content_h = prs.slide_height - 2 * self.top
        self.font_scale = font_scale
        self.slide = None
        self.y = self.top
        # 与 prs.slides 平行：每张流式页贡献了哪些源页（片3 修订的映射依据）
        self.metas: list[dict] = []

    def note_page(self, page_index: int) -> None:
        if self.metas:
            self.metas[-1]["pages"].add(page_index)

    @property
    def content_w_pt(self) -> float:
        return self.content_w / _EMU_PER_PT

    @property
    def content_h_pt(self) -> float:
        return self.content_h / _EMU_PER_PT

    def _new_slide(self) -> None:
        self.slide = self.prs.slides.add_slide(self.blank)
        self.metas.append({"pages": set(), "mode": "flow"})
        self.y = self.top

    def start_section(self) -> None:
        """一级标题/被整页忠实页打断后：当前页有内容就另起一页"""
        if self.slide is not None and self.y > self.top:
            self._new_slide()

    def _ensure(self, height: int) -> None:
        if self.slide is None or self.y - self.top + height > self.content_h:
            self._new_slide()

    def _advance(self, height: int) -> None:
        self.y += height + int(_FLOW_GAP_PT * _EMU_PER_PT)

    def fs(self, pt: float) -> float:
        return pt * self.font_scale

    def text(self, text: str, font: float, *, bold: bool) -> None:
        if not text:
            return
        for piece in _split_text_pieces(text, font, self.content_w_pt, self.content_h_pt):
            lines = _count_lines(piece, font, self.content_w_pt)
            pitch = _LINE_PITCH_SINGLE if lines == 1 else _LINE_PITCH_MULTI
            h = int(lines * font * pitch * _EMU_PER_PT)
            self._ensure(h)
            _add_textbox(
                self.slide, piece, (self.margin, self.y, self.content_w, h), max_pt=font, bold=bold
            )
            self._advance(h)

    def heading(self, text: str, level: int) -> None:
        if level == 1:
            self.start_section()
            self.text(text, self.fs(_FLOW_TITLE_PT), bold=True)
            return
        # 节标题孤儿护栏：剩不足标题+两行正文就换页，避免标题独悬页底
        head_pt = self.fs(_FLOW_HEAD_PT)
        lines = _count_lines(text, head_pt, self.content_w_pt)
        pitch = _LINE_PITCH_SINGLE if lines == 1 else _LINE_PITCH_MULTI
        h = int(lines * head_pt * pitch * _EMU_PER_PT)
        self._ensure(h + int(2 * self.fs(_FLOW_BODY_PT) * _LINE_PITCH_MULTI * _EMU_PER_PT))
        _add_textbox(
            self.slide,
            text,
            (self.margin, self.y, self.content_w, h),
            max_pt=head_pt,
            bold=True,
        )
        self._advance(h)

    def image(self, blob: bytes | None) -> None:
        size = _image_native_size(blob) if blob else None
        if size is None:
            self.text("（图片缺失）", 12.0, bold=False)
            return
        nw, nh = size
        scale = min(
            self.content_w / nw,
            self.content_h * _FLOW_IMAGE_MAXH / nh,
            _FLOW_IMAGE_MAX_UPSCALE_EMU,
        )
        w, h = int(nw * scale), int(nh * scale)
        self._ensure(h)
        left = self.margin + (self.content_w - w) // 2
        self.slide.shapes.add_picture(io.BytesIO(blob), left, self.y, width=w, height=h)
        self._advance(h)

    def table(self, content: dict, images: dict) -> None:
        html = content.get("html")
        parsed = parse_html_table(html) if html else None
        if parsed is None:
            path = (content.get("image_source") or {}).get("path")
            blob = images.get(path.rsplit("/", 1)[-1]) if path else None
            if blob is not None:
                self.image(blob)
            else:
                self.text("（表格解析失败）", 12.0, bold=False)
            return
        n_rows = parsed[0]
        table_font = self.fs(_FLOW_TABLE_PT)
        row_h = table_font * 1.6
        total = int(n_rows * row_h * _EMU_PER_PT)
        if total <= self.content_h:
            self._ensure(total)
            _place_native_table(
                self.slide, parsed, (self.margin, self.y, self.content_w, total), table_font
            )
            self._advance(total)
            return
        # 整页装不下：带跨格的表回落渲染图（跨格不能跨页拆），否则按行拆成多张原生表
        if parsed[3]:
            path = (content.get("image_source") or {}).get("path")
            blob = images.get(path.rsplit("/", 1)[-1]) if path else None
            self.image(blob)
            return
        font = self.fs(12.0)
        per = max(1, int(self.content_h_pt // (font * 1.6)))
        anchors = parsed[2]
        for start in range(0, n_rows, per):
            chunk_rows = min(per, n_rows - start)
            sub_anchors = {
                (r - start, c): t
                for (r, c), t in anchors.items()
                if start <= r < start + chunk_rows
            }
            self._new_slide()
            h = int(chunk_rows * font * 1.6 * _EMU_PER_PT)
            _place_native_table(
                self.slide,
                (chunk_rows, parsed[1], sub_anchors, []),
                (self.margin, self.y, self.content_w, h),
                font,
            )
            self.y += h + int(_FLOW_GAP_PT * _EMU_PER_PT)


def _is_dense(blocks: list) -> bool:
    n = 0
    visual = 0.0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        n += 1
        bbox = b.get("bbox")
        if b.get("type") in _VISUAL_KINDS and bbox and len(bbox) == 4:
            visual += (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6
    return visual >= _DENSE_VISUAL_AREA or n > _DENSE_BLOCK_COUNT


def _flow_block(deck: _FlowDeck, block: dict, images: dict) -> None:
    kind = block.get("type")
    content = block.get("content") or {}
    if kind == "title":
        deck.heading(_block_text(block), content.get("level", 2))
    elif kind == "table":
        deck.table(content, images)
    elif kind in _VISUAL_KINDS:
        path = (content.get("image_source") or {}).get("path")
        deck.image(images.get(path.rsplit("/", 1)[-1]) if path else None)
    else:
        deck.text(_block_text(block), deck.fs(_FLOW_BODY_PT), bold=False)
    # 片2：VLM 修复过的块必须让用户看得出来（防幻觉的透明度要求）
    if block.get("repaired"):
        deck.text("（LLM 修复）", 10.0, bold=False)


def render_blocks_to_pptx(pages, images, page_sizes=None, *, mode: str = "auto") -> bytes:
    """把 MinerU 块产物渲染成 .pptx（兼容签名，实现见 render_blocks_with_meta）"""
    data, _meta = render_blocks_with_meta(pages, images, page_sizes, mode=mode)
    return data


def render_blocks_with_meta(
    pages,
    images,
    page_sizes=None,
    *,
    mode: str = "auto",
    overrides: dict[int, str] | None = None,
    font_scale: float = 1.0,
) -> tuple[bytes, list[dict]]:
    """渲染并返回 (pptx 字节, 每张幻灯片的来源元数据)。

    Args:
        pages: content_list，外层页列表、内层块列表
        images: {basename: bytes}，块 image_source.path 按 basename 索引
        page_sizes: 每页 (宽pt, 高pt)；缺省退 A4
        mode: auto=逐页分流（横向/密集→A 整页忠实，其余→B 阅读顺序重排）；
            page=强制 A；flow=强制 B（密集页仍兜底 A）
        overrides: 片3 版面修订用——{页号: "page"/"flow"} 强制覆盖分流决策
        font_scale: 片3 版面修订用——全局字号缩放（<1 缓解溢出/堆叠）

    meta 与幻灯片顺序一致：{"pages": 源页号元组, "mode": "page"/"flow"}，
    是版面批评循环把"被点名的幻灯片"映射回可调旋钮（换分流 / 缩字号）的依据。
    """
    prs = Presentation()
    slide_w, slide_h = prs.slide_width, prs.slide_height
    blank = prs.slide_layouts[6]
    deck = _FlowDeck(prs, font_scale)
    for i, blocks in enumerate(pages):
        page_w, page_h = page_sizes[i] if page_sizes and i < len(page_sizes) else _DEFAULT_PAGE_PT
        # 横向页=幻灯导出件：页眉即页标题、页脚是品牌标，保留才完整
        noise = _ALWAYS_NOISE if page_w > page_h else _ALWAYS_NOISE | _PORTRAIT_NOISE
        use_page = mode == "page" or (mode != "flow" and page_w > page_h) or _is_dense(blocks)
        if overrides and i in overrides:
            use_page = overrides[i] == "page"
        if use_page:
            slide = prs.slides.add_slide(blank)
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") in noise:
                    continue
                bbox = block.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                box = _bbox_to_emu(bbox, page_w, page_h, slide_w, slide_h)
                _render_block(slide, block, images, box, font_scale)
                if block.get("repaired"):
                    label = (
                        box[0],
                        box[1] + box[3],
                        min(box[2], int(120 * _EMU_PER_PT)),
                        int(14 * _EMU_PER_PT),
                    )
                    _add_textbox(slide, "LLM 修复", label, max_pt=9, bold=False)
            deck.metas.append({"pages": {i}, "mode": "page"})
            # 整页忠实页插断了流：下一块流式内容必须另起页，不能接回插断前的旧页
            deck.slide = None
        else:
            # 流式重排里页眉页脚一律是运行噪声（A 模保留横向页眉脚是因为那本来就是页标题）
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in _ALWAYS_NOISE | _PORTRAIT_NOISE:
                    continue
                _flow_block(deck, block, images)
                deck.note_page(i)
    if not prs.slides:
        slide = prs.slides.add_slide(blank)
        _add_textbox(slide, "（无内容）", (0, 0, slide_w, slide_h), max_pt=18, bold=False)
        deck.metas.append({"pages": set(), "mode": "flow"})
    buf = io.BytesIO()
    prs.save(buf)
    meta = [{"pages": tuple(sorted(m["pages"])), "mode": m["mode"]} for m in deck.metas]
    return buf.getvalue(), meta
