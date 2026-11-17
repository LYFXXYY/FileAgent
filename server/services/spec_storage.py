"""规范文档入库链路：二进制 → 提取正文 → 切块 → 落库

【为什么这一层独立成 service】
路由层管 HTTP 语义（状态码、隔离校验、参数解析），service 管业务转换。
阶段 6 加检索时，路由不变，service 里再叠一层 embedding 即可。

【切块边界 = 阶段 3 同一份 chunking】
`split_document` 的三条不变式（块文本=原文连续切片、块首尾相接、逐字拼回原文）
对规范库同样成立。规范文档阶段 6 检索命中后要做"指回原文位置"的展示，
偏移必须和正文严丝合缝——所以这里不另写一套，**直接复用**。

【失败语义】
- 提取失败（PDF 没文字层 / docx 损坏）：抛 SpecStorageError，路由转 400。
- 切块失败（预算太小）：chunking 自己抛 ChunkError，service 接住再抛 SpecStorageError。
- 空文档（提取出来一个字都没有）：抛 SpecStorageError，避免建一份"啥都没有"的规范库。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from config import get_settings
from models.spec_doc import SpecChunk, SpecDoc
from services.chunking import ChunkError, split_document
from services.pdf import PdfError, extract_text_from_pdf
from services.word import extract_text_from_docx

# 与 api/v1/process.py 同一份白名单语义。两处都派生，加格式时同步改
TEXT_EXTRACTORS = {
    ".docx": extract_text_from_docx,
    ".pdf": extract_text_from_pdf,
}


class SpecStorageError(Exception):
    """入库链路对路由层的统一异常出口。message 是给前端看的中文，不外抛堆栈"""


def ingest_spec_doc(
    db: Session,
    *,
    user_id: int,
    title: str,
    filename: str,
    file_bytes: bytes,
) -> SpecDoc:
    """
    把一份规范文档落到 `spec_docs` + `spec_chunks`

    单事务：要么 doc + 全部 chunk 一起 commit，要么异常回滚不留半成品。
    """
    ext = _normalize_ext(filename)
    extractor = TEXT_EXTRACTORS.get(ext)
    if extractor is None:
        raise SpecStorageError(f"不支持的格式 {ext}，目前只接 .docx 与 .pdf")

    try:
        text = extractor(file_bytes)
    except PdfError as e:
        raise SpecStorageError(f"PDF 提取失败：{e}") from e
    except Exception as e:  # docx 损坏等
        raise SpecStorageError(f"提取正文失败：{e}") from e

    if not text or not text.strip():
        raise SpecStorageError("文档没有可索引的正文（可能是扫描件，PDF 没文字层）")

    budget = get_settings().spec_chunk_chars
    try:
        chunks = split_document(text, max_chars=budget)
    except ChunkError as e:
        raise SpecStorageError(f"切块失败：{e}") from e

    doc = SpecDoc(
        user_id=user_id,
        title=title or _strip_ext(filename),
        source_format=ext,
        char_count=len(text),
        chunk_count=len(chunks),
    )
    db.add(doc)
    db.flush()  # 拿 doc.id 给 chunk 用

    for c in chunks:
        db.add(
            SpecChunk(
                doc_id=doc.id,
                seq=c.index,
                text=c.text,
                start=c.start,
                end=c.end,
            )
        )
    db.commit()
    db.refresh(doc)
    return doc


def delete_spec_doc(db: Session, *, doc: SpecDoc) -> None:
    """删规范文档。chunks 由 ON DELETE CASCADE 带走；老 MySQL 库可能没有这条规则——
    应用层先显式清一遍 chunks 再删 doc，让 SQLite 单测和真库行为一致。"""
    db.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).delete()
    db.delete(doc)
    db.commit()


def _normalize_ext(filename: str) -> str:
    """小写、带前导点。空扩展名直接返回空串让上游拒"""
    if "." not in filename:
        return ""
    return ("." + filename.rsplit(".", 1)[-1]).lower()


def _strip_ext(filename: str) -> str:
    if "." not in filename:
        return filename
    return filename.rsplit(".", 1)[0]
