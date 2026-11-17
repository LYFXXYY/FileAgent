"""
PPTX 二进制拼装（阶段 5 s5.3）—— python-pptx，文字版

【签名硬约束（SPEC §9.12.2.C）】
`build_pptx(outline, *, image_refs=None, template_path=None)` 是图片轮切换实现的入口。
当前 s5.3：image_refs 永远传 None；template_path 留接口但忽略（默认模板）。
阶段 6 / 图片轮真要放图时：image_refs 里 key 是 outline.slides[i].image_placeholders 中的 ID，
value 是 bytes（PNG / JPEG），builder 内部按 placeholder 名插到对应页。

【为什么 image_refs 现在就要在签名里】
学习项目的"留口子"如果只写注释不写代码，下次进来的人会重新设计接口，留口子等于没留。
**接口形状就是设计文档**——这条比"先空着、以后改"明确得多。

【输出形态】
- 第一页是 section header 风格的大标题页（用 outline.title 当主标题、第一页 slide 标题当副标题）；
- 之后每页用 Title and Content 布局：标题 + bullets；
- bullets 空时只占标题位，内容框不动；
- 字号在版式默认里取（python-pptx 内置模板已设 28pt 标题 / 18pt 内容；阶段 5 不调样式）。
"""

from __future__ import annotations

import io
from typing import Any

from pptx import Presentation
from pptx.util import Inches

from schemas.ppt_outline import PptOutline


class PptxError(Exception):
    """PPTX 拼装失败统一出口。路由层接住转 500 或 400"""


def build_pptx(
    outline: PptOutline,
    *,
    image_refs: dict[str, bytes] | None = None,
    template_path: str | None = None,
) -> bytes:
    """
    把 PptOutline 转成 .pptx 二进制。空 outline 也保证返回 1 页"未生成内容"占位

    Args:
        outline: schemas.ppt_outline.PptOutline 实例
        image_refs: 阶段 5 永远传 None；图片轮启用时按 image_placeholders ID 索引
        template_path: 阶段 5 忽略；未来若支持用户母版再读
    """
    # 真用 image_refs 的时候，缺对应 key 就标警告而不是抛——大纲文字仍要可下载
    refs: dict[str, Any] = image_refs or {}

    prs = Presentation()

    # 封面：用 outline.title 做主标题，没标题时退到第一页 slide 标题
    cover_layout = prs.slide_layouts[0]  # Title Slide
    cover = prs.slides.add_slide(cover_layout)
    title_text = outline.title or (outline.slides[0].title if outline.slides else "演示文稿")
    cover.shapes.title.text = title_text
    subtitle_count = max(len(outline.slides) - (1 if outline.title else 0), 0)
    if cover.placeholders and len(cover.placeholders) > 1:
        cover.placeholders[1].text = f"共 {subtitle_count or len(outline.slides)} 页"

    # 正文页：Title and Content 布局
    body_layout = prs.slide_layouts[1]
    for slide_data in outline.slides:
        slide = prs.slides.add_slide(body_layout)
        slide.shapes.title.text = slide_data.title or "（无标题）"
        if slide_data.bullets:
            body = slide.placeholders[1].text_frame
            body.text = slide_data.bullets[0]
            for extra in slide_data.bullets[1:]:
                p = body.add_paragraph()
                p.text = extra
                p.level = 0

        # 图片轮启用时这里按 image_placeholders 找 refs 里的位图、居中铺到右下区域
        for ph_id in slide_data.image_placeholders:
            blob = refs.get(ph_id)
            if blob is None:
                continue
            _insert_image(slide, blob)

    if not outline.slides and not outline.title:
        # 完全空的 outline 走这条占位分支，保证 pptx 不是 0 页（PowerPoint 会拒绝打开）
        placeholder = prs.slides.add_slide(body_layout)
        placeholder.shapes.title.text = "（未生成内容）"

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _insert_image(slide, blob: bytes) -> None:
    """把图按 4 英寸宽放到右下区域。真实图片轮要做布局避让，s5.3 占位"""
    pic_stream = io.BytesIO(blob)
    slide.shapes.add_picture(pic_stream, Inches(6.5), Inches(3.5), width=Inches(4))
