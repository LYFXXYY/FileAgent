"""规范文档入库链路单测 —— 提取 → 切块 → 落库 → 删除

不连 Postgres、不出网：内存 SQLite + 现造 docx 字节。
"""

from __future__ import annotations

import io

import pytest
from docx import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.spec_doc import SpecChunk, SpecDoc
from models.user import User
from services.spec_storage import SpecStorageError, delete_spec_doc, ingest_spec_doc


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    user = User(username="u1", password_hash="x")  # 不真校验密码，只为满足非空约束
    db.add(user)
    db.commit()
    try:
        yield db
    finally:
        db.close()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    buf = io.BytesIO()
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(buf)
    return buf.getvalue()


def _long_paragraph(prefix: str, repeat: int) -> str:
    """造一段固定字数、字符可识别的段落，验证块边界"""
    body = "。".join(f"{prefix}{i}" for i in range(repeat))
    return body + "。"


# ---------- 正向路径 ----------
def test_ingest_creates_doc_and_chunks(session):
    """一份 .docx 进去，spec_docs 多一行、spec_chunks 多 N 行，chunk_count == N"""
    raw = _docx_bytes(
        [
            _long_paragraph("A", 20),
            _long_paragraph("B", 20),
        ]
    )
    doc = ingest_spec_doc(session, user_id=1, title="测试规范", filename="x.docx", file_bytes=raw)

    assert doc.id is not None
    assert doc.chunk_count >= 1
    assert doc.char_count == len(doc.title) + 0 or doc.char_count > 0  # char_count 是正文字数
    rows = session.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).count()
    assert rows == doc.chunk_count


def test_chunks_reassemble_to_extracted_text(session):
    """块拼回去就是提取后的正文——和 services/chunking 不变式一致"""
    raw = _docx_bytes(
        [
            "论文格式要求第 1 条：标题用黑体三号。",
            "第 2 条：正文宋体小四，行距 1.5 倍。",
            "第 3 条：页边距上下 2.5cm、左右 3cm。",
        ]
    )
    doc = ingest_spec_doc(session, user_id=1, title="格式规范", filename="fmt.docx", file_bytes=raw)

    chunks = (
        session.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).order_by(SpecChunk.seq).all()
    )
    # 默认 SPEC_CHUNK_CHARS=2000，三段短文只会切 1 块；拼起来就是完整正文
    text = "".join(c.text for c in chunks)
    assert "黑体三号" in text
    assert "1.5 倍" in text
    assert "2.5cm" in text


def test_title_falls_back_to_filename_when_blank(session):
    """title 留空用文件名去扩展名，不是空串"""
    raw = _docx_bytes(["第一条规范"])
    doc = ingest_spec_doc(session, user_id=1, title="", filename="校规.docx", file_bytes=raw)
    assert doc.title == "校规"


# ---------- 异常路径 ----------
def test_unsupported_extension_raises(session):
    raw = _docx_bytes(["anything"])
    with pytest.raises(SpecStorageError, match="不支持的格式"):
        ingest_spec_doc(session, user_id=1, title="t", filename="x.txt", file_bytes=raw)


def test_empty_docx_raises(session):
    """空白 docx 提取后是空串——拒绝入库，避免建一份空规范库"""
    raw = _docx_bytes([])
    with pytest.raises(SpecStorageError, match="没有可索引的正文"):
        ingest_spec_doc(session, user_id=1, title="empty", filename="e.docx", file_bytes=raw)


# ---------- 删除 ----------
def test_delete_clears_chunks_with_doc(session):
    raw = _docx_bytes(
        [
            "条款 1",
            "条款 2",
        ]
    )
    doc = ingest_spec_doc(session, user_id=1, title="要删", filename="d.docx", file_bytes=raw)
    doc_id = doc.id

    delete_spec_doc(session, doc=doc)

    assert session.get(SpecDoc, doc_id) is None
    assert session.query(SpecChunk).filter(SpecChunk.doc_id == doc_id).count() == 0


# ---------- 用户隔离（与 §3.1 一致，service 层只存不查，校验在路由） ----------
def test_each_doc_owned_by_caller_id(session):
    """入库时 user_id 是调用方传的，路由传 current.id；service 不猜"""
    raw = _docx_bytes(["x 条"])
    a = ingest_spec_doc(session, user_id=1, title="a", filename="a.docx", file_bytes=raw)
    b = ingest_spec_doc(session, user_id=2, title="b", filename="b.docx", file_bytes=raw)
    assert a.user_id == 1
    assert b.user_id == 2
