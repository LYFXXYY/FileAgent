"""
核心业务流程路由：上传 → LLM 处理 → 下载

【流程设计（3 步分离）】
  1. POST /upload            → 上传 .docx → 提取文本 → 建 operation_log(pending) → 返回 log_id
  2. POST /process/{log_id}  → 拼 Prompt → 调 LLM → 更新 log(processed_content, done/failed)
  3. GET  /download/{log_id} → 读 log → 生成 .docx（批改任务标红 diff） → 返回文件

【为什么不合并成一步？】
  - 大文件上传慢，处理更慢（LLM 十几秒），拆开后前端可以实时显示进度
  - 用户可以先看提取的文本对不对，再点处理
  - 处理失败可以只重试 /process 而不用重新上传
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from config import get_settings
from database import get_db
from middleware.auth import get_current_user
from models.operation_log import LogStatus, OperationLog
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.process import ProcessResponse, UploadResponse
from services.llm import LLMError, call_llm
from services.word import create_diff_docx, create_plain_docx, extract_text_from_docx

router = APIRouter()
settings = get_settings()

# 允许的上传扩展名（白名单，防任意文件类型）
ALLOWED_EXTENSIONS = {".docx"}


def _resolve_dir(subdir: str) -> str:
    """把配置里的相对路径解析成绝对路径，并确保存在"""
    path = subdir if os.path.isabs(subdir) else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", subdir
    )
    path = os.path.normpath(path)
    os.makedirs(path, exist_ok=True)
    return path


@router.post("/upload", response_model=UploadResponse, status_code=201)
async def upload(
    task_id: int,
    file: UploadFile = File(..., description="要上传的 .docx 文件"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """
    上传 Word 文档并提取文本

    - task_id 通过 query 参数传（?task_id=1）
    - file 通过 multipart/form-data 传
    """
    # 1. 校验任务存在且属于当前用户
    task = db.query(Task).filter(Task.id == task_id, Task.user_id == current.id).first()
    if task is None:
        raise HTTPException(404, "任务不存在")

    # 2. 校验文件扩展名
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"只允许 {ALLOWED_EXTENSIONS}，当前 {ext!r}")

    # 3. 读取字节 + 提取文本
    file_bytes = await file.read()  # UploadFile.read() 是 async
    try:
        text = extract_text_from_docx(file_bytes)
    except Exception as e:  # noqa: BLE001  解析失败原因多样（损坏、非 docx 等）
        raise HTTPException(400, f"Word 文件解析失败：{e}") from e

    # 4. 落盘（可选：本项目主要用 log 里的 original_content，文件保留供审计）
    stored_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_resolve_dir(settings.upload_dir), stored_name), "wb") as f:
        f.write(file_bytes)

    # 5. 建 pending 日志（provider_name 稍后 process 时补齐）
    log = OperationLog(
        user_id=current.id,
        task_id=task.id,
        task_name=task.name,
        original_filename=file.filename or stored_name,
        original_content=text,
        status=LogStatus.PENDING,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    # 手工构造 UploadResponse —— 不能直接 return log，因为字段名不匹配
    # (UploadResponse.log_id vs OperationLog.id, UploadResponse.content_length 是新增字段)
    return UploadResponse(
        log_id=log.id,
        original_filename=log.original_filename,
        content_length=len(text),
        status=log.status,
        uploaded_at=log.uploaded_at,
    )


@router.post("/process/{log_id}", response_model=ProcessResponse)
async def process(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """
    调用 LLM 处理已上传的文档

    注意 async def —— 因为里面有 await call_llm(...)，整个函数必须异步
    """
    log = db.query(OperationLog).filter(
        OperationLog.id == log_id, OperationLog.user_id == current.id
    ).first()
    if log is None:
        raise HTTPException(404, "记录不存在")
    if log.status not in (LogStatus.PENDING, LogStatus.FAILED):
        raise HTTPException(400, f"当前状态 {log.status.value} 不可处理（仅 pending/failed 可重试）")

    # 关联任务
    task = db.query(Task).filter(Task.id == log.task_id).first()
    if task is None:
        raise HTTPException(400, "关联的任务已被删除")

    # 选 provider：任务指定 > 否则系统第一个 active 的
    provider: ModelProvider | None = None
    if task.provider_id is not None:
        provider = db.query(ModelProvider).get(task.provider_id)

    # 拼 prompt：{{content}} 替换为文档内容
    prompt = task.prompt_template.replace("{{content}}", log.original_content or "")

    # 状态 → processing
    log.status = LogStatus.PROCESSING
    db.commit()

    # 调 LLM
    from services.llm import LLMError as _LLMError  # 局部导入避免循环
    try:
        result = await call_llm(db, prompt, provider)
    except _LLMError as e:
        log.status = LogStatus.FAILED
        log.error_message = str(e)[:2000]  # 截断防超长
        db.commit()
        db.refresh(log)
        return ProcessResponse(
            log_id=log.id,
            status=log.status,
            processed_at=log.processed_at,
            error_message=log.error_message,
        )

    # 成功
    log.processed_content = result
    log.provider_name = provider.name if provider else _default_provider_name(db)
    log.status = LogStatus.DONE
    log.processed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(log)
    return ProcessResponse(
        log_id=log.id,
        status=log.status,
        processed_at=log.processed_at,
        error_message=log.error_message,
    )


@router.get("/download/{log_id}")
async def download(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """下载结果 Word —— 批改任务生成带红字标注的 diff，普通任务直接输出"""
    log = db.query(OperationLog).filter(
        OperationLog.id == log_id, OperationLog.user_id == current.id
    ).first()
    if log is None:
        raise HTTPException(404, "记录不存在")
    if log.status != LogStatus.DONE:
        raise HTTPException(400, f"任务未完成（当前 {log.status.value}）")

    task = db.query(Task).filter(Task.id == log.task_id).first()
    is_grading = bool(task and task.is_grading)

    if is_grading:
        buf = create_diff_docx(log.original_content or "", log.processed_content or "")
        filename = f"批改_{log.original_filename}"
    else:
        buf = create_plain_docx(log.processed_content or "")
        filename = f"结果_{log.original_filename}"

    # StreamingResponse 直接把 BytesIO 流转发给客户端，不需要落盘
    # 类比 Java 的 ResponseEntity<InputStreamResource> + Content-Disposition
    headers = {"Content-Disposition": f'attachment; filename="result.docx"'}
    # 中文文件名要 RFC 5987 编码；filename* 是新版浏览器识别的
    from urllib.parse import quote
    headers["Content-Disposition"] = f"attachment; filename=\"result.docx\"; filename*=UTF-8''{quote(filename)}"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


def _default_provider_name(db: Session) -> str | None:
    """拿到实际使用的默认 provider 名字（当 task 未指定时）"""
    p = (
        db.query(ModelProvider)
        .filter(ModelProvider.is_active == True)  # noqa: E712
        .order_by(ModelProvider.id)
        .first()
    )
    return p.name if p else None
