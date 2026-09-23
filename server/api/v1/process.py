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

import logging
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from config import get_settings
from database import SessionLocal, get_db
from middleware.auth import get_current_user
from models.operation_log import LogStatus, OperationLog
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.process import ProcessResponse, UploadResponse
from services.llm import LLMError, call_llm
from services.word import create_diff_docx, create_plain_docx, extract_text_from_docx

logger = logging.getLogger("fileagent.process")

router = APIRouter()
settings = get_settings()

# 允许的上传扩展名（白名单，防任意文件类型）
ALLOWED_EXTENSIONS = {".docx"}


def _resolve_dir(subdir: str) -> str:
    """把配置里的相对路径解析成绝对路径，并确保存在"""
    path = (
        subdir
        if os.path.isabs(subdir)
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", subdir)
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
    except Exception as e:
        raise HTTPException(400, f"Word 文件解析失败：{e}") from e

    # 4. 落盘（可选：本项目主要用 log 里的 original_content，文件保留供审计）
    # ASYNC230：写的是已在内存里的小 docx，阻塞时间极短；上面的 python-docx 解析同样是
    # 同步阻塞。若以后要支持大文件，应把解析+落盘整体丢进 run_in_threadpool。
    stored_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_resolve_dir(settings.upload_dir), stored_name), "wb") as f:  # noqa: ASYNC230
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

    return UploadResponse(
        log_id=log.id,
        original_filename=log.original_filename,
        content_length=len(text),
        extracted_text=text,
        status=log.status,
        uploaded_at=log.uploaded_at,
    )


@router.post("/process/{log_id}", response_model=ProcessResponse)
async def process(
    log_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """
    触发 LLM 处理已上传的文档 —— 立即返回 processing，实际处理放到后台任务

    【为什么不在请求里 await？】
      LLM 可能耗时十几秒到一两分钟。如果在请求内 await，客户端一旦断开
      （刷新/关页/网络抖动），请求被取消，日志会永远卡在 processing。
      改用 BackgroundTasks：响应先发出去，处理在后台用独立 DB session 跑完，
      前端通过 GET /process/{id}/status 轮询拿结果——断开重连也不影响处理。
    """
    log = (
        db.query(OperationLog)
        .filter(OperationLog.id == log_id, OperationLog.user_id == current.id)
        .first()
    )
    if log is None:
        raise HTTPException(404, "记录不存在")
    if log.status not in (LogStatus.PENDING, LogStatus.FAILED):
        msg = f"当前状态 {log.status.value} 不可处理（仅 pending/failed 可重试）"
        raise HTTPException(400, msg)

    # 关联任务（fail-fast：在返回前就校验，避免后台才发现任务没了）
    task = db.get(Task, log.task_id) if log.task_id is not None else None
    if task is None:
        raise HTTPException(400, "关联的任务已被删除")

    # 任务指定了 provider 时，校验它仍存在且属于当前用户（防越权 + 防静默用错模型）
    if task.provider_id is not None:
        provider = db.get(ModelProvider, task.provider_id)
        if provider is None or provider.user_id != current.id:
            raise HTTPException(400, "任务指定的模型提供商已被删除或不可用，请重新选择")

    # 状态 → processing，清掉上次的错误信息
    log.status = LogStatus.PROCESSING
    log.error_message = None
    db.commit()
    db.refresh(log)

    # 后台执行真正的 LLM 调用（独立 session，不受本请求生命周期影响）
    background_tasks.add_task(_run_processing, log.id)

    return ProcessResponse(
        log_id=log.id,
        status=log.status,
        processed_at=None,
        error_message=None,
        processed_content=None,
    )


async def _run_processing(log_id: int) -> None:
    """
    后台处理：调 LLM 并把结果/错误写回 operation_log

    必须用独立的 SessionLocal()——请求级 session 在响应发出后就关闭了。
    任何异常都兜底标记为 failed，绝不让日志卡在 processing。
    """
    db = SessionLocal()
    try:
        log = db.get(OperationLog, log_id)
        if log is None or log.status != LogStatus.PROCESSING:
            return  # 已被其他流程改动或不存在，直接退出

        task = db.get(Task, log.task_id) if log.task_id is not None else None
        if task is None:
            _mark_failed(db, log, "关联的任务已被删除")
            return

        # 选 provider：任务指定 > 当前用户的默认 active
        provider: ModelProvider | None = None
        if task.provider_id is not None:
            provider = db.get(ModelProvider, task.provider_id)
            if provider is None or provider.user_id != log.user_id:
                _mark_failed(db, log, "任务指定的模型提供商已被删除或不可用，请重新选择")
                return

        prompt = task.prompt_template.replace("{{content}}", log.original_content or "")

        try:
            result = await call_llm(db, prompt, provider, user_id=log.user_id)
        except LLMError as e:
            _mark_failed(db, log, str(e)[:2000])
            return

        log.processed_content = result
        log.provider_name = provider.name if provider else _default_provider_name(db, log.user_id)
        log.status = LogStatus.DONE
        log.processed_at = datetime.now(UTC)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("后台处理 log_id=%s 失败", log_id)
        try:
            log = db.get(OperationLog, log_id)
            if log is not None and log.status == LogStatus.PROCESSING:
                _mark_failed(db, log, f"内部错误：{e}"[:2000])
        except Exception:
            db.rollback()
    finally:
        db.close()


def _mark_failed(db: Session, log: OperationLog, message: str) -> None:
    """把日志标记为 failed 并写入错误信息（统一提交点）"""
    log.status = LogStatus.FAILED
    log.error_message = message[:2000]
    log.processed_at = datetime.now(UTC)
    db.commit()


@router.get("/process/{log_id}/status", response_model=ProcessResponse)
async def process_status(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """查询处理状态 —— 只读接口，不会触发或重复触发 LLM 处理"""
    log = (
        db.query(OperationLog)
        .filter(OperationLog.id == log_id, OperationLog.user_id == current.id)
        .first()
    )
    if log is None:
        raise HTTPException(404, "记录不存在")

    return ProcessResponse(
        log_id=log.id,
        status=log.status,
        processed_at=log.processed_at,
        error_message=log.error_message,
        processed_content=log.processed_content if log.status == LogStatus.DONE else None,
    )


@router.get("/download/{log_id}")
async def download(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """下载结果 Word —— 批改任务生成带红字标注的 diff，普通任务直接输出"""
    log = (
        db.query(OperationLog)
        .filter(OperationLog.id == log_id, OperationLog.user_id == current.id)
        .first()
    )
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
    # 中文文件名要 RFC 5987 编码；filename* 是新版浏览器识别的
    from urllib.parse import quote

    disposition = f"attachment; filename=\"result.docx\"; filename*=UTF-8''{quote(filename)}"
    headers = {"Content-Disposition": disposition}

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


def _default_provider_name(db: Session, user_id: int) -> str | None:
    """拿到实际使用的默认 provider 名字（当 task 未指定时，限当前用户名下）"""
    p = (
        db.query(ModelProvider)
        .filter(ModelProvider.is_active == True, ModelProvider.user_id == user_id)  # noqa: E712
        .order_by(ModelProvider.id)
        .first()
    )
    return p.name if p else None
