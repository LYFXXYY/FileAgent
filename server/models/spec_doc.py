"""
规范文档 ORM（阶段 4）—— 用户私有规范库的存储层

【表语义】
- `spec_docs`：每个用户能存任意份规范文档（论文格式要求、单位手册、课程约定）。
  正文不进这张表，权威在 `spec_chunks.text`，靠 `seq` 顺序拼回。
- `spec_chunks`：按段落切好的检索粒度。阶段 4 只入库不做检索；阶段 6 给这张表加
  embedding 列 + pgvector 索引就能用，不需要再动 schema 主体。

【为什么切块存而不存整篇】
1. 阶段 6 检索是按块召回的，块大小是检索精度的主旋钮；存整篇时每次召回都要现场切，
   边界漂了召回也跟着漂。
2. 一份规范文档几万字起步，整篇塞进 LLM prompt 既不省钱又会被中段折角"lost in middle"
   影响。预先切好块，prompt 只塞 top-k 块。
3. `reassemble(source, chunks, [c.text for c in chunks])` 是 §3.10 已立的不变式，
   阶段 6 用同一份 services/chunking.py 切，规范库和批改链路共享同一套边界规则。

【为什么没有 source_path】
正文权威在 DB，磁盘只作审计留存——和 operation_logs 同一原则。规范库不需要回原文件，
要重传就重传。

【数据隔离】
spec_docs 上挂 user_id；spec_chunks 通过外键间接归属。所有查询都要带
`user_id == current.id`（§3.1）；越权 404 不暴露存在性。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class SpecDoc(Base):
    __tablename__ = "spec_docs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 用户视角的规范库标签，便于以后做选择器；同用户下重名允许
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 用户上传时的扩展名（小写带点），仅作可读提示，正文已转 Text 不再依赖二进制
    source_format: Mapped[str] = mapped_column(String(10), nullable=False, default=".docx")

    # 提取出的整篇字数与块数：列表页只显示这两个标量，不读 chunks
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    chunks: Mapped[list[SpecChunk]] = relationship(
        back_populates="doc",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SpecChunk.seq",
    )

    def __repr__(self) -> str:
        return f"<SpecDoc id={self.id} user={self.user_id} title={self.title!r}>"


class SpecChunk(Base):
    __tablename__ = "spec_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    doc_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("spec_docs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # 文档内的块序号，从 0 起。阶段 6 检索按这个顺序拼上下文
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    # 块内容（原文切片）。Postgres TEXT 没有大小上限
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # 全局偏移，与 services/chunking.Chunk 一一对应。阶段 6 给前端做"指回原文位置"用
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)

    doc: Mapped[SpecDoc] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<SpecChunk id={self.id} doc={self.doc_id} seq={self.seq}>"
