"""
PPTX 拼装单测（阶段 5 s5.3）—— 二进制合法、页数正确、可被 python-pptx 重读

不连 Postgres、不出网；纯内存字节流。
"""

import io

import pytest
from pptx import Presentation

from schemas.ppt_outline import PptOutline, PptSlide
from services.pptx_builder import PptxError, build_pptx


def _outline(*slide_titles_and_bullets: tuple[str, list[str]], title: str = "总标题") -> PptOutline:
    slides = [
        PptSlide(title=t, bullets=b, image_placeholders=[]) for t, b in slide_titles_and_bullets
    ]
    return PptOutline(title=title, slides=slides, degraded=False)


def test_build_pptx_produces_valid_bytes():
    """python-pptx 写出的字节流能被自家库重读，最少证明 PPTX 没坏"""
    outline = _outline(
        ("封面页", ["作者 张三"]),
        ("背景", ["问题 1", "问题 2"]),
    )
    blob = build_pptx(outline)

    assert isinstance(blob, bytes)
    assert len(blob) > 0
    # PK 头是 zip 容器（PPTX 是 OOXML zip 包）
    assert blob[:2] == b"PK"

    prs = Presentation(io.BytesIO(blob))
    # 1 封面 + 2 正文页
    assert len(prs.slides) == 3


def test_cover_uses_outline_title():
    outline = _outline(("第一页要点", ["a"]), title="论文汇报")
    blob = build_pptx(outline)
    prs = Presentation(io.BytesIO(blob))
    first = prs.slides[0]
    assert first.shapes.title.text == "论文汇报"


def test_body_slides_have_titles_and_bullets():
    outline = _outline(
        ("方法", ["思路 A", "思路 B", "思路 C"]),
        ("结果", ["指标 X"]),
        title="t",
    )
    blob = build_pptx(outline)
    prs = Presentation(io.BytesIO(blob))

    # 跳过封面
    slides = list(prs.slides)[1:]
    assert slides[0].shapes.title.text == "方法"
    # body placeholder 在 layout 1 (Title and Content) 的第二个 placeholder
    bullets = [p.text for p in slides[0].placeholders[1].text_frame.paragraphs]
    assert bullets == ["思路 A", "思路 B", "思路 C"]

    assert slides[1].shapes.title.text == "结果"
    assert slides[1].placeholders[1].text_frame.paragraphs[0].text == "指标 X"


def test_empty_outline_yields_one_placeholder_slide():
    """完全空的 outline 也得有 1 页占位，PowerPoint 拒绝打开 0 页文件"""
    outline = PptOutline(title="", slides=[], degraded=True)
    blob = build_pptx(outline)
    prs = Presentation(io.BytesIO(blob))
    # 至少 1 页（封面 + 占位 = 2 页也合理，断言下限 ≥1）
    assert len(prs.slides) >= 1


def test_image_refs_none_is_default():
    """s5.3 永远不传 image_refs；签名必须接受默认值，否则违反 §9.12.2.C 硬约束"""
    outline = _outline(("s1", []))
    blob = build_pptx(outline)  # 不传第二参
    assert len(blob) > 0


def test_image_refs_with_image_inserts_picture():
    """图片轮启用路径——给一张最小合法 PNG，builder 应插到对应页

    这是"留口子"的硬证据：image_refs 字典 + 同名 image_placeholders 触发插图，
    阶段 6 接 PDF 抽取的图片时调用方不用改 build_pptx 签名。
    """
    # 64×64 全黑 PNG，最小合法图像字节流
    import struct
    import zlib

    def _make_png() -> bytes:
        sig = b"\x89PNG\r\n\x1a\n"
        # IHDR
        ihdr_data = struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
        ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
        # IDAT — 64 行，每行 1 字节 filter + 192 字节 RGB
        raw = b"".join(b"\x00" + b"\x00" * 192 for _ in range(64))
        idat_data = zlib.compress(raw)
        idat_crc = zlib.crc32(b"IDAT" + idat_data) & 0xFFFFFFFF
        idat = struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data + struct.pack(">I", idat_crc)
        # IEND
        iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
        iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
        return sig + ihdr + idat + iend

    png = _make_png()
    outline = PptOutline(
        title="t",
        slides=[PptSlide(title="带图页", bullets=["要点"], image_placeholders=["fig_1"])],
        degraded=False,
    )
    blob = build_pptx(outline, image_refs={"fig_1": png})

    prs = Presentation(io.BytesIO(blob))
    # 1 封面 + 1 正文页；正文页有 1 个图片 shape
    body_slide = list(prs.slides)[1]
    pictures = [s for s in body_slide.shapes if s.shape_type == 13]  # MSO_SHAPE_TYPE.PICTURE
    assert len(pictures) == 1


def test_image_refs_unknown_id_skipped_silently():
    """placeholder 写了但 image_refs 字典里没有对应 key——跳过不抛"""
    outline = PptOutline(
        title="t",
        slides=[PptSlide(title="页", bullets=[], image_placeholders=["missing"])],
        degraded=False,
    )
    blob = build_pptx(outline, image_refs={"other": b"x"})
    prs = Presentation(io.BytesIO(blob))
    body_slide = list(prs.slides)[1]
    pics = [s for s in body_slide.shapes if s.shape_type == 13]
    assert len(pics) == 0


# 占位，确保 PptxError 类存在且被导入（builder 当前不抛，但路由层接它要类存在）
def test_pptx_error_class_importable():
    assert issubclass(PptxError, Exception)


@pytest.mark.parametrize("n", [1, 3, 5])
def test_page_count_matches_outline_size(n):
    slides = [(f"页 {i}", [f"要点 {i}"]) for i in range(n)]
    outline = _outline(*slides)
    blob = build_pptx(outline)
    prs = Presentation(io.BytesIO(blob))
    # 封面 1 页 + 正文 n 页
    assert len(prs.slides) == n + 1
