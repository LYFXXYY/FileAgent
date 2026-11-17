"""
规范文档路由（阶段 4）—— 上传、列表、读块、删除

【数据隔离（§3.1）】
- 每条查询都带 `user_id == current.id`，spec_docs 是用户私有库。
- 越权访问一律 404，不暴露"存在但不属于你"。
- chunks 不直接挂 user_id，必须经 doc 校验后再读，绝不绕过。

【和文档处理链路的关系】
两条独立链路：
- `/upload` + `/process` 处理用户的工作文档，产物是 diff 与结果；
- `/specs/upload` 入库规范文档（论文格式要求等），阶段 6 之前没有 LLM 调用，只到落库。

阶段 6 加检索后，规范库会在批改链路里以"召回的条款 + 引用编号"形式注入 prompt。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from middleware.auth import get_current_user
from models.spec_doc import SpecChunk, SpecDoc
from models.user import User
from schemas.spec import SpecChunkResponse, SpecDocResponse
from services.spec_storage import (
    TEXT_EXTRACTORS,
    SpecStorageError,
    delete_spec_doc,
    ingest_spec_doc,
)

router = APIRouter()

# 与 api/v1/process.py 同源派生，加格式时两处一起看（也意味着 spec_storage 是白名单真源）
ALLOWED_EXTENSIONS = set(TEXT_EXTRACTORS)


@router.post("/upload", response_model=SpecDocResponse, status_code=201)
def upload_spec(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """multipart 上传一份规范文档；title 留空时用文件名去扩展名"""
    filename = file.filename or ""
    ext = ("." + filename.rsplit(".", 1)[-1]).lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的格式 {ext or '(无扩展名)'}，目前只接 .docx 与 .pdf",
        )

    raw = file.file.read()
    try:
        doc = ingest_spec_doc(
            db,
            user_id=current.id,
            title=title,
            filename=filename,
            file_bytes=raw,
        )
    except SpecStorageError as e:
        # 提取/切块失败都属于"内容问题"，400 不带堆栈
        raise HTTPException(status_code=400, detail=str(e)) from e
    return doc


@router.get("", response_model=list[SpecDocResponse])
def list_specs(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """只列当前用户的规范文档。chunks 字段不在这里，列表页只取标量"""
    return db.query(SpecDoc).filter(SpecDoc.user_id == current.id).order_by(SpecDoc.id.desc()).all()


@router.get("/{doc_id}/chunks", response_model=list[SpecChunkResponse])
def get_spec_chunks(
    doc_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """读规范文档的全部块。先校验 doc 归属再读 chunks，越权 404 不区分 doc 还是 chunk"""
    doc = db.query(SpecDoc).filter(SpecDoc.id == doc_id, SpecDoc.user_id == current.id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="规范文档不存在")
    return db.query(SpecChunk).filter(SpecChunk.doc_id == doc.id).order_by(SpecChunk.seq).all()


@router.delete("/{doc_id}", status_code=204)
def delete_spec(
    doc_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """删规范文档。chunks 由应用层显式清，不依赖 PG CASCADE 的具体规则"""
    doc = db.query(SpecDoc).filter(SpecDoc.id == doc_id, SpecDoc.user_id == current.id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="规范文档不存在")
    delete_spec_doc(db, doc=doc)
    return None
