"""规范文档入库链路单测 —— 提取 → 切块 → 落库 → 删除

不连 Postgres、不出网：内存 SQLite + 现造 docx 字节。
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from docx import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.spec_doc import SpecChunk, SpecDoc
from models.user import User
from services.embedding import EmbeddingError
from services.spec_storage import SpecStorageError, delete_spec_doc, ingest_spec_doc


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    """
    假编码：不出网。

    入库链路现在默认会算向量（spec_embed_on_ingest=true），不桩掉它这些测试全部会
    因为"embedding 端点未配置"红掉。桩返回可辨识的短向量——SQLite 不校验列宽，
    真库上维度不符由 services/embedding.py 自己拦。
    """
    seen: list[list[str]] = []

    def _fake(texts):
        seen.append(list(texts))
        return [[float(len(t)), 0.5] for t in texts]

    monkeypatch.setattr("services.spec_storage.embed_texts", _fake)
    return seen


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


# ---------- 规范库不可变（阶段 6 前置约束 4） ----------
def test_reingest_same_title_creates_new_doc_not_update(session):
    """
    改了规范就重传，旧文档整份留着。

    这条约束是为 embedding 准备的：一旦支持 UPDATE，旧向量会和新正文共存，
    检索会命中已经作废的条款——而那种错在结果里看不出痕迹。
    """
    raw = _docx_bytes(["1.1.1 同音别字", "【判定标准】替换后语义不通即判定。"])

    first = ingest_spec_doc(
        session, user_id=1, title="同一份规范", filename="a.docx", file_bytes=raw
    )
    old_chunks = session.query(SpecChunk).filter(SpecChunk.doc_id == first.id).all()
    old_texts = [c.text for c in old_chunks]

    second = ingest_spec_doc(
        session, user_id=1, title="同一份规范", filename="b.docx", file_bytes=raw
    )

    assert second.id != first.id
    assert session.query(SpecDoc).filter(SpecDoc.title == "同一份规范").count() == 2
    # 旧文档一块没动
    now = [c.text for c in session.query(SpecChunk).filter(SpecChunk.doc_id == first.id).all()]
    assert now == old_texts


def test_module_exposes_no_update_path(session):
    """入库模块只有 ingest / delete 两个写入口，没有原地改的口子"""
    import services.spec_storage as m

    public = {n for n in dir(m) if not n.startswith("_") and callable(getattr(m, n))}
    assert not {n for n in public if n.lower().startswith(("update", "edit", "modify"))}


def test_chunks_follow_clause_boundaries(session):
    """
    入库切块按条款走：三条条款就该是三块，且每块都带着【判定标准】。

    这条直接钉住 2026-11-19 实测到的腰斩缺陷（旧写法 2000 字装箱会把判据切到下一块）。
    """
    raw = _docx_bytes(
        [
            "1.1.1 同音别字",
            "【适用情形】读音相同但含义不同。",
            "【判定标准】替换后语义不通即判定。",
            "【错误示例】再接再励。",
            "【正确示例】再接再厉。",
            "1.1.2 形近别字",
            "【适用情形】字形相近的字写错。",
            "【判定标准】核对部首与词义。",
            "【错误示例】提纲很草。",
            "【正确示例】提纲很差。",
            "2.1.1 语序不当",
            "【适用情形】定语顺序不合习惯。",
            "【判定标准】按领属、数量、动词短语排列。",
            "【错误示例】他很学习努力。",
            "【正确示例】他学习很努力。",
        ]
    )
    doc = ingest_spec_doc(session, user_id=1, title="条款规范", filename="c.docx", file_bytes=raw)
    chunks = (
        session.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).order_by(SpecChunk.seq).all()
    )

    assert len(chunks) == 3
    assert all("【判定标准】" in c.text for c in chunks)
    assert [c.start for c in chunks] == sorted(c.start for c in chunks)


# ---------- 向量入库（阶段 6 A1） ----------
def test_ingest_writes_embedding_per_chunk(session, fake_embedder):
    """每块都拿到向量，且编码收到的文本就是那些块文本——顺序一致、不漏块"""
    raw = _docx_bytes(["1.1.1 同音别字", "【判定标准】替换后语义不通即判定。"])
    doc = ingest_spec_doc(session, user_id=1, title="向量规范", filename="v.docx", file_bytes=raw)

    chunks = session.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).all()
    assert chunks
    assert all(c.embedding is not None for c in chunks)
    assert fake_embedder == [[c.text for c in chunks]]


def test_embed_failure_aborts_whole_ingest(session, monkeypatch):
    """
    编码失败 = 这份规范根本没进来。

    规范库没有 UPDATE 口子，"文本进了、向量为 NULL"是补不回来的静默缺陷：
    列表页显示已入库，检索却永远看不见这些块。所以必须整份失败。
    """

    def _boom(texts):
        raise EmbeddingError("连不上 embedding 端点")

    monkeypatch.setattr("services.spec_storage.embed_texts", _boom)
    before_docs = session.query(SpecDoc).count()
    before_chunks = session.query(SpecChunk).count()

    raw = _docx_bytes(["1.1.1 同音别字", "【判定标准】替换后语义不通即判定。"])
    with pytest.raises(SpecStorageError, match="向量编码失败"):
        ingest_spec_doc(session, user_id=1, title="进不来", filename="f.docx", file_bytes=raw)

    assert session.query(SpecDoc).count() == before_docs
    assert session.query(SpecChunk).count() == before_chunks


def test_embed_disabled_leaves_vectors_null(session, fake_embedder, monkeypatch):
    """关掉开关时只存文本，向量交给回填脚本——这是给"先建库后接端点"留的口子"""
    monkeypatch.setattr(
        "services.spec_storage.get_settings",
        lambda: SimpleNamespace(spec_chunk_chars=2000, spec_embed_on_ingest=False),
    )
    raw = _docx_bytes(["1.1.1 同音别字", "【判定标准】替换后语义不通即判定。"])
    doc = ingest_spec_doc(session, user_id=1, title="纯文本", filename="n.docx", file_bytes=raw)

    chunks = session.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).all()
    assert chunks
    assert all(c.embedding is None for c in chunks)
    assert fake_embedder == []  # 一次外呼都没发
