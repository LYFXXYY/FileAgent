"""
图片型 Word 的 Markdown 兜底（md 转换任务的唯一例外支路）

【为什么有它】md 转换的 docx 路径本来是"本地 python-docx，零外呼"（AGENTS §3.16）。
但整份文档的文字长在图片里时，本地提取出来是空的，用户拿到的是一条
"文档没有可转换的正文内容"——文档明明有内容，只是不在文本流里。

【不变式的边界，改这里前先读三遍】
- 只有 `needs_image_fallback()` 成立（本地正文 < 40 字 **且** 文档里确实有图片）才走这里。
  有正常文字的 Word 仍然一行外呼都不发，"md 转换零 LLM"对绝大多数文档照旧成立。
- 外呼只走 **vision** 能力的提供商，由调用方用 `resolve_provider(user, None, "vision")`
  解析（不接受 provider_id 直传，AGENTS §3.3 capability 路由的延伸），
  计量 purpose 是 `md_image`。
- **MinerU 不参与这条支路**：我们的 docx→md 从来没把 docx 送过 MinerU
  （`services/mineru.py:parse_pdf_via_mineru` 只吃 PDF 字节）。所以"MinerU 不可用"
  不是这里的开关，而是失败文案里给用户的替代建议——把 Word 另存为 PDF 再转，
  那条路是真的会走 MinerU 版面识别的。

【顺序与保真】图片按 OPC 关系表顺序取，不保证与正文位置一一对应；这是"内容能拿出来"
的兜底，不是版面保真转换。矢量图（EMF/WMF）视觉模型吃不了，只计数不送。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from collections.abc import Sequence

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from services.llm import LLMResult, chat

logger = logging.getLogger("fileagent.docx_image_md")

# 本地正文少于此字数才认定"文字都在图片里"。阈值不是为了卡 40 字，
# 而是为了放过"正文正常 + 夹了几张插图"的常见文档——那种情况不该花钱。
MIN_TEXT_CHARS = 40

# 单份文档最多送几张图：再多就是账单黑洞，而且真有几十字页扫描图的文档该走 PDF 路线
MAX_IMAGES = 12

# 小于这个体积的图基本是项目 logo、分隔线、页眉装饰，识别出来只会污染结果
MIN_IMAGE_BYTES = 8 * 1024

# 视觉模型能直接吃的位图类型（OpenAI 兼容端点的 image_url 约定）
_RASTER_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}

# 只能计数、送不出去的类型
_VECTOR_TYPES = ("image/x-emf", "image/x-wmf", "image/emf", "image/wmf")

TRANSCRIBE_SYSTEM = (
    "你在把文档里的图片转成 Markdown。规则："
    "1) 只输出图片中的文字内容与结构，标题用 #、列表用 -、表格用 Markdown 表格语法；"
    "2) 不要评论、不要总结、不要解释你在做什么，也不要加开场白；"
    "3) 辨认不出来的字符写 …，不要编造；"
    "4) 图片里没有文字时，只输出一行 [图片：无文字内容]。"
)


def list_images(file_bytes: bytes) -> tuple[list[tuple[str, bytes]], int]:
    """
    取出 docx 里能送进视觉模型的位图，返回 `[(扩展名, 字节), ...]` 与"送不出去的矢量图张数"

    按内容哈希去重（同一张图在页眉里重复出现是常态），并过滤掉小体积装饰图。
    """
    doc = Document(io.BytesIO(file_bytes))
    seen: set[str] = set()
    images: list[tuple[str, bytes]] = []
    vector_count = 0

    for rel in doc.part.rels.values():
        if rel.reltype != RT.IMAGE:
            continue
        try:
            part = rel.target_part
        except Exception:
            continue

        blob = getattr(part, "blob", b"") or b""
        content_type = (getattr(part, "content_type", "") or "").lower()

        if any(v in content_type for v in _VECTOR_TYPES):
            vector_count += 1
            continue
        ext = _RASTER_TYPES.get(content_type)
        if ext is None or len(blob) < MIN_IMAGE_BYTES:
            continue

        digest = hashlib.sha1(blob).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        images.append((ext, blob))

    return images, vector_count


def needs_image_fallback(markdown: str, images: Sequence[tuple[str, bytes]]) -> bool:
    """本地正文几乎为空 **且** 文档里确实有位图 —— 才允许发第一次外呼"""
    return len(markdown.strip()) < MIN_TEXT_CHARS and bool(images)


async def transcribe_images(
    images: Sequence[tuple[str, bytes]],
    provider,
    on_result=None,
) -> tuple[list[str], list[str]]:
    """
    逐图请视觉模型转写，返回 `(每图的 Markdown 片段, 失败说明)`

    **单图失败不整体失败**（AGENTS §3.7"用户的文档必须拿到一个结果"的延伸）：
    一张识别失败就在结果里留一句说明，其余照常交付。

    `on_result` 是调用方传的计量回调（异步），每张图成功或失败都要调一次——
    本模块只发请求、不碰 DB（AGENTS §3.6 传输层边界）。
    """
    chunks: list[str] = []
    notes: list[str] = []

    for index, (ext, blob) in enumerate(images[:MAX_IMAGES], start=1):
        data_url = f"data:image/{ext};base64," + base64.b64encode(blob).decode()
        messages = [
            {"role": "system", "content": TRANSCRIBE_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"第 {index} 张图片，请转成 Markdown。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        try:
            result: LLMResult = await chat(messages, provider)
        except Exception as e:
            logger.warning("图片转写失败 第 %d 张：%r", index, e)
            notes.append(f"第 {index} 张图片识别失败（{type(e).__name__}）")
            if on_result is not None:
                await on_result(None, str(e), 1)
            continue

        if on_result is not None:
            await on_result(result, None, result.attempts)

        text = (result.content or "").strip()
        if not text:
            notes.append(f"第 {index} 张图片没有返回内容")
            continue
        chunks.append(text)

    if len(images) > MAX_IMAGES:
        notes.append(f"共 {len(images)} 张图片，只识别了前 {MAX_IMAGES} 张，其余请拆分文档后再转")

    return chunks, notes


def compose_markdown(local_text: str, chunks: Sequence[str], vector_count: int) -> str:
    """把本地提取到的少量文字（标题之类）与逐图转写结果拼成一份 md"""
    parts = [local_text.strip()] if local_text.strip() else []
    for index, chunk in enumerate(chunks, start=1):
        parts.append(f"## 图片 {index}\n\n{chunk}")
    if vector_count:
        parts.append(
            f"> 另有 {vector_count} 张矢量图（EMF/WMF）无法识别，"
            "建议在 Word 里另存为位图后重转，或把文档另存为 PDF 走版面识别。"
        )
    return "\n\n".join(parts) + "\n"
