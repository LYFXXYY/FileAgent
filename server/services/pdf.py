"""
PDF 文本提取服务 —— pymupdf（PyMuPDF 2.x）

【为什么不是 page.get_text("text") 一把梭】
docx 那边一个段落就是一行，后端的 diff、批改定位、docx 生成全建立在
「\\n 分隔的段落」这个约定上。PDF 的文本层只给视觉换行，不给段落边界：
- Word/LaTeX 导出的件，一个自然段是一个 block，内部按行以 \\n 分隔 → 要拼回一段
- PPT/幻灯导出的件，**每视觉行各占一个 block** → 不合并就是一句话被切成七八"段"，
  diff 会把半句当整段，问题清单指到换行处
所以做两遍：block 内按行拼，block 间按几何邻接拼。产出的文本和 docx 同构，
下游一行代码都不用区分来源。

【用 pymupdf 而不是 fitz】PyMuPDF 2.x 里 `import fitz` 会打 FutureWarning 并声明将来移除，
1.x 时代的老代码全是 fitz，本项目直接按新名走。

【已知边界，别当 bug 查】
- 扫描版 PDF 没有文字层，提出来是空的 → 明确报错，不做 OCR（属阶段 5）
- 加密 PDF 直接拒绝，不支持输密码
- 多栏排版的 block 顺序不等于阅读顺序，长段落跨栏时仍会被切开：栏与栏左边缘不同，
  几何合并刻意不跨栏（跨了更糟），所以顺序问题留到阶段 5 用版面分析解决
- 页眉页脚只按"页码 + 跨页高频"两条规则剔除，花哨的装饰文字会留在正文里

【Java 对比】
Java 侧等价物是 PDFBox 的 `PDFTextStripper`：它默认也按内容流顺序吐字，
要按位置排序得显式 `setSortByPosition(true)`，而且它同样不理解段落——只给一行一行。
PDFBox 3.x 的 `page.getTextBlocks()` 才给带坐标的块，和本文件用的
`page.get_text("blocks")` 是同一抽象层级。也就是说"块 ≠ 段落"不是 PyMuPDF 的坑，
是 PDF 文本层的固有信息量不足：规范里文本对象只管画在哪，不管属于哪个自然段。
iText / poi-tl 那类库根本不提供免费文本抽取，因为它们都在同一层之上。
"""

from __future__ import annotations

import re
from collections import Counter

import pymupdf

# 行尾出现这些字符说明语义已经收束，下一行是新单位（段落 / 标题 / 列表项），不合并
_TERMINAL_RE = re.compile(r"[。．！？；：.!?;:]\s*$")

# 光秃秃的页码：阿拉伯数字带可选短横，或单独的罗马数字
_PAGE_NUMBER_RE = re.compile(r"^[-–—]?\d{1,4}[-–—]?$|^[IVXLCDM]{1,7}$")

# 中日韩及全角标点：拼行时两侧都是 CJK 就不该插空格
_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

# 带句末标点的重复行按内容对待，不当装饰删
_SENTENCE_PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?]")

# 相邻块的垂直间距阈值（pt）。正常行距下同一行的两块间隙≈0，段落间距有 4pt 以上
_MERGE_GAP_PT = 5.0
# 左边缘对齐容差（pt），超过就认为是另一栏
_ALIGN_TOLERANCE_PT = 3.0
# 页码只可能出现在这个位置：页面上 8% 或下 20% 的留白带
_HEADER_BAND = 0.08
_FOOTER_BAND = 0.8
# 跨页重复判装饰噪声：至少这么长的文档才启用，且重复文本不能太长
_NOISE_MIN_PAGES = 3
_NOISE_MIN_RATIO = 0.6
_NOISE_MAX_LEN = 20


class PdfError(Exception):
    """PDF 无法处理时抛出，让路由层转成 400 而不是 500"""


def _join_block_lines(text: str) -> str:
    """
    把 block 内的视觉换行拼回一个段落

    行尾连字符 + 下行小写开头 → 英文断词，去掉连字符直接接上；
    两侧都是中日韩 → 直接接上（CJK 换行本就没有空格）；
    其余（拉丁文字、中英混排）→ 补一个空格。
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""

    merged = lines[0]
    for right in lines[1:]:
        if merged.endswith("-") and right[:1].islower():
            merged = merged[:-1] + right
        elif _CJK_RE.search(merged[-1]) and _CJK_RE.search(right[0]):
            merged += right
        else:
            merged += " " + right
    return merged


def _is_page_number(paragraph: str, top: float, page_height: float) -> bool:
    """
    是不是页码块：内容光是数字/罗马数字，**并且**落在页面上下留白带里

    少了位置条件会把正文里的年份、条款号、表格序号一起删掉（"2024" 正好是四位数）。
    页码不会出现在版心中间，所以要两条同时成立才丢。
    """
    if not _PAGE_NUMBER_RE.match(paragraph.replace(" ", "")):
        return False
    return top < page_height * _HEADER_BAND or top > page_height * _FOOTER_BAND


def _page_paragraphs(page: pymupdf.Page) -> list[tuple[str, float, float, float]]:
    """
    一页 → [(段落文本, 左边缘, 上边, 下边)]，已丢掉图片块和页码块

    文本块元组是 (x0, y0, x1, y1, text, block_no, block_type)，block_type 1 是图片。
    坐标留着是给下一步判"这两块是不是同一段"。
    """
    page_height = page.rect.height
    out: list[tuple[str, float, float, float]] = []
    for block in page.get_text("blocks"):
        if block[6] != 0:
            continue
        paragraph = _join_block_lines(block[4])
        if not paragraph or _is_page_number(paragraph, block[1], page_height):
            continue
        out.append((paragraph, block[0], block[1], block[3]))
    return out


def _noise_texts(pages: list[list[tuple[str, float, float, float]]]) -> set[str]:
    """
    跨页高频重复的短句 = 页眉页脚装饰（running header、校名页脚那类）

    判据用频次而不是硬编码字符串：换个模板装饰词就变了，但"每页都有一模一行"不变。
    少于 3 页的文档不启用，且带句末标点的一律放过——否则诗词副歌、
    重复的免责声明会被当噪声删掉，丢内容的代价比留一行装饰大得多。
    """
    if len(pages) < _NOISE_MIN_PAGES:
        return set()

    freq: Counter[str] = Counter()
    for page in pages:
        # 同一页里重复出现的短行是内容，跨页都出现才像模板
        for text in {t for t, *_ in page}:
            freq[text] += 1

    threshold = max(_NOISE_MIN_PAGES, round(len(pages) * _NOISE_MIN_RATIO))
    return {
        text
        for text, count in freq.items()
        if count >= threshold
        and len(text) <= _NOISE_MAX_LEN
        and not _SENTENCE_PUNCT_RE.search(text)
    }


def _should_merge(prev: str, prev_bottom: float, prev_left: float, cur: tuple) -> bool:
    """
    当前块要不要接在上一段后面：贴得够近、左边缘对齐、且上一段语义没收束

    三条同时成立才合，缺一条就当新段落 —— 宁可切开，不要把两段缝成一段。
    """
    if _TERMINAL_RE.search(prev):
        return False
    _text, left, top, _bottom = cur
    close_enough = top - prev_bottom <= _MERGE_GAP_PT
    same_column = abs(left - prev_left) <= _ALIGN_TOLERANCE_PT
    return close_enough and same_column


def _merge_blocks(paragraphs: list[tuple[str, float, float, float]]) -> list[str]:
    """把一页里被切成多块的同一自然段重新接起来"""
    if not paragraphs:
        return []

    merged: list[str] = [paragraphs[0][0]]
    prev_left, prev_bottom = paragraphs[0][1], paragraphs[0][3]
    for cur in paragraphs[1:]:
        if _should_merge(merged[-1], prev_bottom, prev_left, cur):
            merged[-1] = _append_text(merged[-1], cur[0])
        else:
            merged.append(cur[0])
        prev_left, prev_bottom = cur[1], cur[3]
    return merged


def _append_text(prev: str, nxt: str) -> str:
    """段落间接行：CJK 直接接，其余补空格（和 block 内拼行同一套规则）"""
    if _CJK_RE.search(prev[-1]) and _CJK_RE.search(nxt[0]):
        return prev + nxt
    return prev + " " + nxt


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """
    从 PDF 字节流提取纯文本，段落之间用 \\n 分隔（与 docx 提取结果同构）。

    抛 PdfError 的三种情况：文件打不开、设了打开密码、没有文字层。
    最后一种最常见于扫描件，必须说清楚，而不是回一段空文本让用户以为文档是空的。
    """
    try:
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        raise PdfError(f"PDF 打开失败：{e}") from e

    try:
        if doc.needs_pass:
            raise PdfError("PDF 设置了打开密码，当前不支持加密文档")

        pages = [_page_paragraphs(page) for page in doc]
        noise = _noise_texts(pages)
        paragraphs: list[str] = []
        for page in pages:
            kept = [item for item in page if item[0] not in noise]
            paragraphs.extend(_merge_blocks(kept))
    finally:
        doc.close()

    if not any(p.strip() for p in paragraphs):
        raise PdfError(
            "PDF 里没有可提取的文字层（多半是扫描件或纯图片导出）。"
            "当前不支持图片型 PDF，请先转成带文字层的 PDF，或改用 .docx"
        )

    return "\n".join(paragraphs)
