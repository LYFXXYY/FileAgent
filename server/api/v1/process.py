"""
核心业务流程路由：上传 → LLM 处理 → 下载

【流程设计（3 步分离）】
  1. POST /upload            → 上传文档 → 提取文本 → 建 operation_log(pending) → 返回 log_id
  2. POST /process/{log_id}  → 拼 Prompt → 调 LLM → 更新 log(processed_content, done/failed)
  3. GET  /download/{log_id} → 读 log → 生成 .docx（批改任务标红 diff） → 返回文件

【为什么不合并成一步？】
  - 大文件上传慢，处理更慢（LLM 十几秒），拆开后前端可以实时显示进度
  - 用户可以先看提取的文本对不对，再点处理
  - 处理失败可以只重试 /process 而不用重新上传
"""

from __future__ import annotations

import io
import logging
import os
import uuid
import zipfile
from dataclasses import dataclass
from datetime import timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import get_settings
from database import SessionLocal, db_now, get_db
from graphs.grading_graph import grade_long_document, read_progress
from middleware.auth import get_current_user
from models.operation_log import LogStatus, OperationLog
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.grading import GRADING_JSON_SCHEMA, GradingIssue, GradingResult
from schemas.process import ChunkProgressResponse, ProcessResponse, UploadResponse
from services.docx_image_md import (
    compose_markdown,
    list_images,
    needs_image_fallback,
    transcribe_images,
)
from services.docx_md import docx_to_markdown
from services.errors import (
    CODE_DOC_EMPTY,
    CODE_DOCX_MD,
    CODE_FILE_MISSING,
    CODE_FILE_READ,
    CODE_INTERNAL,
    CODE_LLM_FAILED,
    CODE_MINERU_FAILED,
    CODE_MINERU_UNREACHABLE,
    CODE_OCR_EMPTY,
    CODE_PDF_MD,
    CODE_PDF_PPT,
    CODE_PPT_DELIVERY,
    CODE_PPT_RENDER,
    CODE_QUOTA_PROCESS,
    CODE_TASK_GONE,
    describe_exception,
    tag,
)
from services.grading import (
    build_messages,
    deserialize_issues,
    deserialize_refs,
    is_degraded,
    parse_grading_output,
    serialize_result,
)
from services.grading import (
    spec_note as read_spec_note,
)
from services.llm import (
    LLMError,
    LLMResult,
    chat,
    chat_json,
    record_llm_call,
    resolve_provider,
)
from services.mineru import MinerUError, parse_pdf_via_mineru
from services.ocr import markdown_to_plain_text, save_structured
from services.pdf import PdfError, ScannedPdfError, extract_text_from_pdf
from services.ppt_convert import PptConvertError, render_blocks_with_meta
from services.ppt_layout import run_layout_loop
from services.ppt_repair import find_defects, repair_defective_blocks
from services.spec_retrieval import SpecContext, collect_spec_context
from services.storage import resolve_dir
from services.uploads import check_pdf_pages, read_capped
from services.word import create_diff_docx, create_plain_docx, extract_text_from_docx

logger = logging.getLogger("fileagent.process")

router = APIRouter()
settings = get_settings()

# 未启用规范检索时的空上下文。用常量而不是到处传 None，`_run_model` 里就能直接读
# spec.context / spec.refs，不必每处判空（判空漏一处就是 AttributeError 级的白崩）
_NO_SPEC = SpecContext(context="", refs=[], note=None)

# 支持的输入格式 → 文本提取器。白名单直接由这张表的键派生，
# 免得加了格式却忘了改白名单（或反过来），两处对不上就是 500。
TEXT_EXTRACTORS = {
    ".docx": extract_text_from_docx,
    ".pdf": extract_text_from_pdf,
}
ALLOWED_EXTENSIONS = set(TEXT_EXTRACTORS)

# error_message 列的长度上限。只在 _mark_failed 一处截，调用方不要再各自 [:2000]——
# 双重截断既看不出真实上限，又会让人以为改一处就够了。
_ERROR_MESSAGE_MAX_CHARS = 2000


@router.post("/upload", response_model=UploadResponse, status_code=201)
async def upload(
    task_id: int,
    file: UploadFile = File(..., description="要上传的文档（.docx / .pdf）"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """
    上传文档并提取文本

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

    # md 任务的 PDF 一律走 MinerU（文字型也要版面→md），没配 token 就在上传时
    # 响亮拒绝，而不是让用户等到处理阶段才失败。docx 走本地转换不受此限
    if task.output_format == "md" and ext == ".pdf" and not settings.mineru_token:
        raise HTTPException(400, "PDF 转 Markdown 需要服务端配置 MINERU_TOKEN")

    # PPT 转换只收 PDF（docx 没有版面块可搬），且全量走 MinerU——同 md 的 fail-fast
    if task.output_format == "pptx":
        if ext != ".pdf":
            raise HTTPException(400, "PDF 转 PPT 只支持 .pdf 输入")
        if not settings.mineru_token:
            raise HTTPException(400, "PDF 转 PPT 需要服务端配置 MINERU_TOKEN")

    # 3. 读取字节 + 按扩展名派发到对应的提取器
    #    分块读到上限就断，不整份吸进内存；PDF 再单独过页数闸（按页计费的东西）
    file_bytes = await read_capped(file)  # UploadFile.read() 是 async
    check_pdf_pages(file_bytes)
    ocr_pending = False
    try:
        text = TEXT_EXTRACTORS[ext](file_bytes)
    except ScannedPdfError as e:
        # 扫描件兜底：配了 MinerU token 才受理，正文推迟到处理阶段 OCR。
        # 没配 token 时行为与接入前逐字一致（400 + 原文案）
        if not settings.mineru_token:
            raise HTTPException(400, str(e)) from e
        text = None
        ocr_pending = True
    except PdfError as e:
        # 加密 / 损坏等 PdfError 的文案本身就是给用户看的，直接透出
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"文档解析失败：{e}") from e

    # 4. 落盘（可选：本项目主要用 log 里的 original_content，文件保留供审计）
    # ASYNC230：写的是已在内存里的小 docx，阻塞时间极短；上面的 python-docx 解析同样是
    # 同步阻塞。若以后要支持大文件，应把解析+落盘整体丢进 run_in_threadpool。
    stored_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(resolve_dir(settings.upload_dir), stored_name), "wb") as f:  # noqa: ASYNC230
        f.write(file_bytes)

    # 5. 建 pending 日志（provider_name 稍后 process 时补齐）。
    # 扫描件受理时 original_content 留 NULL + needs_ocr=True，处理阶段先 OCR 写回
    log = OperationLog(
        user_id=current.id,
        task_id=task.id,
        task_name=task.name,
        original_filename=file.filename or stored_name,
        stored_filename=stored_name,
        original_content=text,
        needs_ocr=ocr_pending,
        status=LogStatus.PENDING,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    return UploadResponse(
        log_id=log.id,
        original_filename=log.original_filename,
        content_length=len(text) if text else 0,
        extracted_text=text or "",
        ocr_pending=ocr_pending,
        status=log.status,
        uploaded_at=log.uploaded_at,
    )


def _quota_error(db: Session, user_id: int) -> str | None:
    """
    近 24 小时处理次数是否已超上限；超了返回给用户看的文案，没超返回 None

    `max_processes_per_day = 0` 表示不限制，此时一条 SQL 都不多问。
    判据取 `processed_at` 落在窗口内、且状态已到 processing/done/failed 的日志——
    上传本身不花钱，只有真正触发过处理才算成本。重试会计入，因为它确实又烧一遍。
    """
    limit = settings.max_processes_per_day
    if limit <= 0:
        return None
    cutoff = db_now(db) - timedelta(hours=24)
    used = (
        db.query(OperationLog)
        .filter(
            OperationLog.user_id == user_id,
            OperationLog.status.in_([LogStatus.PROCESSING, LogStatus.DONE, LogStatus.FAILED]),
            OperationLog.processed_at >= cutoff,
        )
        .count()
    )
    if used < limit:
        return None
    return tag(
        f"最近 24 小时已处理 {used} 次，达到上限 {limit} 次。"
        "请稍后再试，或联系维护者调整 MAX_PROCESSES_PER_DAY",
        CODE_QUOTA_PROCESS,
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

    # 成本闸放在任何状态变更之前：超配额时这条日志必须原样停在 pending，
    # 否则用户看到 429 但日志已经变 processing，前端会一直轮询一个不会跑的任务。
    # 查库丢线程池 + 自开 session：本路由是 async def，直接在事件循环上跑同步
    # psycopg 就等于给每个"开始处理"请求加一次可能阻塞全站的机会；
    # 配额关闭时连线程池都不跳，默认配置零额外开销。
    quota = None
    if settings.max_processes_per_day > 0:

        def _check_quota():
            session = SessionLocal()
            try:
                return _quota_error(session, current.id)
            finally:
                session.close()

        quota = await run_in_threadpool(_check_quota)
    if quota:
        raise HTTPException(429, quota)

    # 关联任务（fail-fast：在返回前就校验，避免后台才发现任务没了）
    task = db.get(Task, log.task_id) if log.task_id is not None else None
    if task is None:
        raise HTTPException(400, "关联的任务已被删除")

    # 任务指定了 provider 时提前校验（防越权 + 防静默用错模型）：
    # 让问题在返回 processing 之前就以 400 暴露，而不是后台跑一半才失败。
    # md/pptx 转换零 LLM，不解析 provider，跳过此校验
    if task.provider_id is not None and task.output_format not in ("md", "pptx"):
        try:
            resolve_provider(db, current.id, task.provider_id, "text")
        except LLMError as e:
            raise HTTPException(400, str(e)) from e

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
        ocr_pending=log.needs_ocr,
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
            _mark_failed(db, log, "关联的任务已被删除", code=CODE_TASK_GONE)
            return

        # md 转换：零 LLM，在选 provider / OCR 兜底之前分流。
        # 扫描件 md 任务上传时也带 needs_ocr=True，由本分支自己走 MinerU，
        # 不进下面那条"OCR 后还要调 LLM"的批改兜底路径
        if task.output_format == "md":
            await _run_md_conversion(db, log)
            return

        # PPT 转换：同样零 LLM（片1），MinerU 版面块 → 块驱动渲染器。
        # 文字型 PDF 也走 MinerU——要的是标题层级/表格/图片块，不是纯文本
        if task.output_format == "pptx":
            await _run_ppt_conversion(db, log)
            return

        # 扫描件兜底：正文还在 MinerU 云端没取回来，先 OCR 写回再走 LLM。
        # 放在选 provider 之前：OCR 失败时错误信息更直接（不用先查一圈模型）。
        # parse_pdf_via_mineru 是同步阻塞（含轮询 sleep），必须丢线程池
        if log.needs_ocr:
            if not log.stored_filename:
                _mark_failed(db, log, "扫描件落盘记录缺失，请重新上传", code=CODE_FILE_MISSING)
                return
            stored_path = os.path.join(resolve_dir(settings.upload_dir), log.stored_filename)
            try:
                with open(stored_path, "rb") as f:  # noqa: ASYNC230
                    pdf_bytes = f.read()
                result = await run_in_threadpool(
                    parse_pdf_via_mineru, pdf_bytes, log.original_filename
                )
            except (MinerUError, OSError) as e:
                _mark_failed(db, log, f"扫描件识别失败：{e}", code=CODE_MINERU_FAILED)
                return
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                # 传输层异常（代理配置非法 / 连接中断等）文案对用户不可行动，
                # 归一成 MinerU 口径；细节进日志供排查
                logger.warning("MinerU 传输层异常 log_id=%s：%r", log.id, e)
                _mark_failed(
                    db,
                    log,
                    "扫描件识别失败：无法连接 MinerU 服务，请检查网络后重试",
                    code=CODE_MINERU_UNREACHABLE,
                )
                return
            plain = markdown_to_plain_text(result.markdown)
            if not plain.strip():
                _mark_failed(
                    db,
                    log,
                    "扫描件识别结果为空，请确认文档内容清晰可辨",
                    code=CODE_OCR_EMPTY,
                )
                return
            log.original_content = plain
            log.needs_ocr = False
            db.commit()
            # structured 留存是旁路：写盘失败只告警，不把已拿到的正文判成失败
            save_structured(
                result,
                os.path.join(resolve_dir(settings.output_dir), "mineru", str(log.id)),
            )

        # 选 provider：任务指定 > 当前用户的默认 active（按 text 能力筛）
        try:
            provider = resolve_provider(db, log.user_id, task.provider_id, "text")
        except LLMError as e:
            _mark_failed(db, log, str(e), code=e.code)
            return

        # 快照成普通值：计量写入要丢线程池，不能跨线程用这个 session 的 ORM 对象
        provider_ref = (provider.id, provider.name)

        original_text = log.original_content or ""

        # 规范条款召回（阶段 6）。只在批改任务时尝试，且两条路径分开：
        # - 单发：整篇查一次，条款进那一条 user 消息
        # - 分块：按块查（长文档各段涉及的条款不同，全局 top-k 等于只按开头检索），
        #   查询能力以闭包传进图，节点自己不碰 DB
        # 检索失败不把已能跑通的批改判成失败：collect_spec_context 内部兜住并回传 note
        spec = _NO_SPEC
        spec_lookup = None
        if task.is_grading:
            if _should_chunk(original_text):
                spec_lookup = _open_spec_lookup(log.user_id)
            else:
                spec = await run_in_threadpool(
                    collect_spec_context,
                    db,
                    user_id=log.user_id,
                    document_text=original_text,
                )

        try:
            output = await _run_model(
                task,
                provider,
                task.prompt_template,
                original_text,
                log.id,
                spec=spec,
                spec_lookup=spec_lookup,
            )
        except LLMError as e:
            await _meter(
                log,
                provider_ref,
                purpose=_error_purpose_for(task),
                error=e.detail,
                attempts=e.attempts,
            )
            _mark_failed(db, log, str(e), code=e.code)
            return

        for call in output.calls:
            await _meter(log, provider_ref, purpose=call.purpose, result=call.result)

        # 记实际选中的 provider 名，不再事后回查默认提供商（回查会记错）
        _mark_done(
            db,
            log,
            processed_content=output.revised_text,
            structured_output=output.structured,
            provider_name=provider_ref[1],
        )
    except Exception as e:
        db.rollback()
        logger.exception("后台处理 log_id=%s 失败", log_id)
        try:
            log = db.get(OperationLog, log_id)
            if log is not None and log.status == LogStatus.PROCESSING:
                code, human = describe_exception(e)
                _mark_failed(db, log, human, code=code)
        except Exception:
            db.rollback()
            # 连"标成 failed"都写不下去（DB 此时多半已不可用）。不记日志的话
            # 现场就只剩一条停在 processing 的僵尸行，排查时没有任何线索。
            logger.exception("写入 failed 状态本身也失败，log_id=%s 将由启动期 reaper 兜底", log_id)
    finally:
        db.close()


def _mark_failed(
    db: Session, log: OperationLog, message: str, *, code: str = CODE_INTERNAL
) -> None:
    """
    把日志标记为 failed 并写入错误信息（统一提交点，长度上限只在这一处截）

    **编号一律由这里追加**，调用点只写人话。给不出具体 code 的落 `E-INTERNAL`——
    它表示"这条失败还没被归类"，本身就是有用的信号，比界面上出现"有时有号有时没号"好。
    """
    log.status = LogStatus.FAILED
    log.error_message = tag(message, code)[:_ERROR_MESSAGE_MAX_CHARS]
    # 时间列全站由 DB 时钟生成（uploaded_at 是 func.now()）。这里若用应用侧 now()，
    # 容器与宿主机的钟差会让 processed_at 反而早于 uploaded_at，耗时算出负数
    log.processed_at = func.now()
    db.commit()
    db.refresh(log)


def _mark_done(db: Session, log: OperationLog, **fields: object) -> None:
    """
    把日志标成 done 并写回结果字段（统一提交点，与 _mark_failed 对称）

    为什么必须收成一个入口：done 收尾原先在批改 / md / pptx 三条链路各手写一遍
    `status + processed_at + commit + refresh`。其中 `processed_at` 必须是 SQL 的
    `func.now()` 而不是应用侧 datetime——容器钟比宿主机快 3–4 秒，写应用时钟就会
    出现 processed_at 早于 uploaded_at、前端"处理耗时"相减得负（§7.3 实拍过的缺陷）。
    这类"漏一行不会报错、只会静默给出错数据"的约定，写三遍就是三个可以漏的地方。

    字段名必须真的是 OperationLog 的列：拼错一个字母会静默变成一个不会被 commit 的
    实例属性——那比手写五件套更危险，所以在这里把它变成响亮错误。校验先整体过一遍
    再赋值，免得第 3 个字段拼错时前 2 个已经脏了当前 session。
    """
    columns = log.__mapper__.columns
    unknown = [name for name in fields if name not in columns]
    if unknown:
        raise ValueError(f"OperationLog 没有这些列：{', '.join(unknown)}，拒绝静默丢弃字段")
    for name, value in fields.items():
        setattr(log, name, value)
    log.status = LogStatus.DONE
    log.processed_at = func.now()
    db.commit()
    # processed_at 赋的是 SQL 表达式，commit 后 refresh 才能拿到 DB 时钟的真实值
    db.refresh(log)


async def _md_docx_via_vision(
    db: Session,
    log: OperationLog,
    local_text: str,
    images: list[tuple[str, bytes]],
    vector_count: int,
) -> str | None:
    """
    图片型 Word 的视觉兜底：返回拼好的 markdown；返回 None 表示已把日志标成 failed

    没有 vision 提供商时**不能静默跳过**——跳过就等于交付一份空 md，用户会以为文档
    本来就没内容（§3.8"扫描件绝不回空文本"同一条理由）。所以这里给的是可行动文案 +
    两条替代路径（用户 2026-11-23 拍板：只有 MinerU 用不上且模型没有视觉能力，
    才允许返回"不可处理"）。
    """
    try:
        provider = resolve_provider(db, log.user_id, None, "vision")
    except LLMError as e:
        _mark_failed(
            db,
            log,
            "这份 Word 的正文都在图片里，本地转换提取不到文字，"
            "而当前账号还没有支持视觉（vision）的模型，无法识别图片内容。"
            "建议：① 在「模型提供商」添加一个能力含 vision 的模型后重试；"
            "② 或在 Word 里「另存为 PDF」后重新上传——PDF 会走 MinerU 版面识别，"
            "图片里的文字也能取出来。",
            code=e.code,
        )
        return None

    provider_ref = (provider.id, provider.name)

    async def _on_result(result, error, attempts):
        await _meter(
            log, provider_ref, purpose="md_image", result=result, error=error, attempts=attempts
        )

    chunks, notes = await transcribe_images(images, provider, on_result=_on_result)
    if not chunks:
        detail = f" 失败详情：{notes[0]}" if notes else ""
        _mark_failed(
            db,
            log,
            f"图片识别没有拿到任何内容（这份文档里有 {len(images)} 张可识别图片）。"
            "建议换一份图片更清晰的文档，或把 Word 另存为 PDF 后重转。" + detail,
            code=CODE_LLM_FAILED,
        )
        return None

    # 真发生过外呼就写上模型名，界面与计量口径一致（同 PPT 支路的做法）
    log.provider_name = provider.name
    if notes:
        logger.warning("md 图片兜底部分失败 log_id=%s：%s", log.id, "；".join(notes))
    markdown = compose_markdown(local_text, chunks, vector_count)
    if notes:
        markdown += "\n" + "\n".join(f"> {n}" for n in notes) + "\n"
    return markdown


async def _run_md_conversion(db: Session, log: OperationLog) -> None:
    """
    md 转换任务的后台处理：docx 走本地转换（免费），PDF 一律走 MinerU。

    零 LLM 对**有文字的文档**成立：不选 provider、不写 llm_calls。唯一例外是
    "Word 的正文都在图片里"这一条支路（`_md_docx_via_vision`），它会用 vision 模型
    逐图转写并记 `md_image` 计量——不这么做的话用户拿到的是一条"没有可转换的正文"，
    而文档明明有内容。processed_content 存 markdown 原文（含表格与图片引用）；
    PDF 路径同时把归一化纯文本写回 original_content，前端预览与批改路径同源。
    """
    if not log.stored_filename:
        _mark_failed(db, log, "文件落盘记录缺失，请重新上传", code=CODE_FILE_MISSING)
        return
    stored_path = os.path.join(resolve_dir(settings.upload_dir), log.stored_filename)
    try:
        with open(stored_path, "rb") as f:  # noqa: ASYNC230
            file_bytes = f.read()
    except OSError as e:
        _mark_failed(db, log, f"读取上传文件失败：{e}", code=CODE_FILE_READ)
        return

    ext = os.path.splitext(log.stored_filename)[1].lower()
    if ext == ".docx":
        try:
            markdown = await run_in_threadpool(docx_to_markdown, file_bytes)
        except Exception as e:
            _mark_failed(db, log, f"Word 转 Markdown 失败：{e}", code=CODE_DOCX_MD)
            return
        images, vector_count = await run_in_threadpool(list_images, file_bytes)
        if needs_image_fallback(markdown, images):
            # 正文都在图片里：这是 md 转换**唯一**会外呼的支路，见 services/docx_image_md.py
            markdown = await _md_docx_via_vision(db, log, markdown, images, vector_count)
            if markdown is None:
                return
        elif not markdown.strip():
            _mark_failed(db, log, "文档没有可转换的正文内容", code=CODE_DOC_EMPTY)
            return
        log.processed_content = markdown
    else:
        # 上传白名单保证这里只剩 .pdf；文字型也走 MinerU——要的是版面→md，
        # 不是纯文本（pymupdf 给不出标题层级/表格/图片引用）
        try:
            result = await run_in_threadpool(
                parse_pdf_via_mineru, file_bytes, log.original_filename
            )
        except (MinerUError, OSError) as e:
            _mark_failed(db, log, f"PDF 转 Markdown 失败：{e}", code=CODE_PDF_MD)
            return
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            logger.warning("MinerU 传输层异常 log_id=%s：%r", log.id, e)
            _mark_failed(
                db,
                log,
                "PDF 转 Markdown 失败：无法连接 MinerU 服务，请检查网络后重试",
                code=CODE_MINERU_UNREACHABLE,
            )
            return
        if not result.markdown.strip():
            _mark_failed(db, log, "识别结果为空，请确认文档内容清晰可辨", code=CODE_OCR_EMPTY)
            return
        log.processed_content = result.markdown
        log.original_content = markdown_to_plain_text(result.markdown)
        # structured 留存是旁路（图片给下载 zip 与后续 PPT 带图用），写盘失败只告警
        save_structured(
            result,
            os.path.join(resolve_dir(settings.output_dir), "mineru", str(log.id)),
        )

    _mark_done(db, log, needs_ocr=False)


async def _run_ppt_conversion(db: Session, log: OperationLog) -> None:
    """
    PDF→PPT 忠实转换的后台处理：MinerU 版面块 → （片2 VLM 坏块修复）→ 渲染 →（片3 版面循环）。

    零**文本** LLM 不变式保留：不解析 text provider、structured_output=NULL
    （重试时顺带清掉历史大纲信封）。VLM 只在两种旁路增强里出现——坏块修复（ppt_repair）
    与版面批评循环（ppt_critic），都要求"有坏块/循环开启 且 配了 vision 提供商"；
    否则 llm_calls 依旧为 0。交付是处理阶段落盘的 pptx 二进制
    （outputs/pptx/{log_id}.pptx，片3 修订后就地覆盖），下载路由直接读；
    processed_content 存 markdown 供预览/审计。
    """
    if not log.stored_filename:
        _mark_failed(db, log, "文件落盘记录缺失，请重新上传", code=CODE_FILE_MISSING)
        return
    stored_path = os.path.join(resolve_dir(settings.upload_dir), log.stored_filename)
    try:
        with open(stored_path, "rb") as f:  # noqa: ASYNC230
            pdf_bytes = f.read()
    except OSError as e:
        _mark_failed(db, log, f"读取上传文件失败：{e}", code=CODE_FILE_READ)
        return

    try:
        result = await run_in_threadpool(parse_pdf_via_mineru, pdf_bytes, log.original_filename)
    except (MinerUError, OSError) as e:
        _mark_failed(db, log, f"PDF 转 PPT 失败：{e}", code=CODE_PDF_PPT)
        return
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        logger.warning("MinerU 传输层异常 log_id=%s：%r", log.id, e)
        _mark_failed(
            db,
            log,
            "PDF 转 PPT 失败：无法连接 MinerU 服务，请检查网络后重试",
            code=CODE_MINERU_UNREACHABLE,
        )
        return
    if not result.content_list:
        _mark_failed(db, log, "识别结果为空，请确认文档内容清晰可辨", code=CODE_OCR_EMPTY)
        return

    # vision 提供商有两个消费者：片2 坏块修复 + 片3 版面循环。
    # 先本地判坏（零成本）；无坏块且版面循环关闭时根本不查找。
    # 两者都是旁路增强——没配 vision、VLM 挂了、产出没过校验，都保留片1 兜底继续交付
    defects = find_defects(result.content_list)
    layout_wanted = settings.ppt_layout_enabled
    pages_for_render = result.content_list
    repair_calls: list = []
    vision_ref: tuple[int | None, str | None] | None = None
    vision_provider = None
    if defects or layout_wanted:
        try:
            vision_provider = resolve_provider(db, log.user_id, None, "vision")
        except LLMError:
            vision_provider = None
            logger.info("用户 %s 没有 vision 提供商，PPT 坏块修复与版面循环跳过", log.user_id)
        if vision_provider is not None:
            # 快照成普通值：计量写线程池不能跨线程用 ORM 对象（§3.6 边界）
            vision_ref = (vision_provider.id, vision_provider.name)
            if defects:
                pages_for_render, repair_calls = await repair_defective_blocks(
                    pdf_bytes, result.content_list, result.page_sizes, vision_provider, log.id
                )

    try:
        pptx_bytes, slide_meta = await run_in_threadpool(
            render_blocks_with_meta, pages_for_render, result.images, result.page_sizes
        )
    except PptConvertError as e:
        _mark_failed(db, log, f"PPT 渲染失败：{e}", code=CODE_PPT_RENDER)
        return

    # 片3 版面检查循环：只调分流/字号两个旋钮，文字一字不动。
    # 循环内部已兜住外呼与渲染失败，这里的 try 只防"循环自身有 bug"这种事故
    layout_calls: list = []
    if vision_provider is not None and layout_wanted:
        try:
            pptx_bytes, _final_meta, layout_calls = await run_layout_loop(
                pages_for_render,
                result.images,
                result.page_sizes,
                vision_provider,
                log.id,
                first_data=pptx_bytes,
                first_meta=slide_meta,
                max_rounds=settings.ppt_layout_max_rounds,
            )
        except Exception:
            logger.exception("版面循环内部异常 log_id=%s，交付当前版本", log.id)
            layout_calls = []

    out_dir = resolve_dir(os.path.join(settings.output_dir, "pptx"))
    try:
        with open(os.path.join(out_dir, f"{log.id}.pptx"), "wb") as f:  # noqa: ASYNC230
            f.write(pptx_bytes)
    except OSError as e:
        _mark_failed(
            db,
            log,
            f"PPT 交付文件写入失败：{e}",
            code=CODE_PPT_DELIVERY,
        )
        return

    # 修复/批评外呼逐笔记账（成功/降级/失败都有行），计量本身仍是旁路不影响交付
    for call in repair_calls:
        await _meter(
            log,
            vision_ref,
            purpose="ppt_repair_degraded" if call.degraded else "ppt_repair",
            result=call.result,
            error=call.error,
            attempts=call.attempts,
        )
    for call in layout_calls:
        await _meter(
            log,
            vision_ref,
            purpose="ppt_critic_degraded" if call.degraded else "ppt_critic",
            result=call.result,
            error=call.error,
            attempts=call.attempts,
        )

    log.processed_content = result.markdown
    # 扫描件上传时没有预览文本，用归一化纯文本补底本；文字型保留上传时的提取结果
    if not log.original_content:
        log.original_content = markdown_to_plain_text(result.markdown)
    # 只有真发生过 VLM 外呼才记模型名——"无需模型"的语义对没坏块也没跑循环的文档仍然成立
    _mark_done(
        db,
        log,
        structured_output=None,
        provider_name=vision_ref[1] if (repair_calls or layout_calls) else None,
        needs_ocr=False,
    )
    # structured 留存是旁路（片2 坏块修复要拿区域原图 grounding），写盘失败只告警
    save_structured(
        result,
        os.path.join(resolve_dir(settings.output_dir), "mineru", str(log.id)),
    )


async def _meter(
    log: OperationLog,
    provider_ref: tuple[int | None, str | None],
    *,
    purpose: str,
    result: LLMResult | None = None,
    error: str | None = None,
    attempts: int = 1,
) -> None:
    """
    把一次 LLM 外呼记进 llm_calls

    record_llm_call 是同步的（SQLAlchemy Session 是同步的），直接 await 调用会把
    事件循环卡住，所以丢线程池。计量写失败由 record_llm_call 内部吞掉，不影响这里。
    """
    await run_in_threadpool(
        record_llm_call,
        user_id=log.user_id,
        provider_id=provider_ref[0],
        provider_name=provider_ref[1],
        log_id=log.id,
        purpose=purpose,
        result=result,
        error=error,
        attempts=attempts,
    )


@dataclass(frozen=True)
class _MeteredCall:
    """一笔要写进 llm_calls 的真实外呼"""

    result: LLMResult
    purpose: str


@dataclass(frozen=True)
class _ModelOutput:
    """
    一次处理的产出

    `calls` 是**列表**而不是单个结果：分块批改一轮会打 N 次模型，每次都要单独记账，
    不然长文档的成本在 llm_calls 里只有一行，看起来跟短文档一样便宜。
    """

    revised_text: str
    structured: str | None
    calls: tuple[_MeteredCall, ...]


def _error_purpose_for(task: Task) -> str:
    """LLMError 也要按任务种类归类，否则评测台按 purpose 分组时分不清"""
    return "grading" if task.is_grading else "chat"


def _should_chunk(original_text: str) -> bool:
    """
    只有批改类才分块（调用方已判过 is_grading），且长到超预算

    摘要、翻译、润色不分块：它们的产出需要对**整篇**负责，逐块做完再拼只会得到
    N 份局部摘要，比一次性塞进去更差。上限不够就报错，也不假装能分块。
    """
    budget = settings.longdoc_chunk_chars
    return bool(settings.longdoc_enabled) and len(original_text) > budget


def _open_spec_lookup(user_id: int):
    """
    给分块批改图用的按块条款查询闭包（阶段 6 A6）。

    每次调用自开一个 session：图会并发跑多个块，跨线程共用同一个 Session 不安全，
    而且请求级 session 早就关了——和 §3.3 里 `record_llm_call()` 同一个理由。
    返回 (上下文, refs, note)，节点侧只拿这三样，不认识 DB。
    """

    async def lookup(chunk_text: str) -> tuple[str, list[dict], str | None]:
        def _work():
            db = SessionLocal()
            try:
                return collect_spec_context(db, user_id=user_id, document_text=chunk_text)
            finally:
                db.close()

        ctx = await run_in_threadpool(_work)
        return ctx.context, ctx.refs, ctx.note

    return lookup


async def _run_model(
    task: Task,
    provider: ModelProvider,
    prompt_template: str,
    original_text: str,
    log_id: int,
    *,
    spec: SpecContext | None = None,
    spec_lookup=None,
) -> _ModelOutput:
    """
    两条路径：批改走结构化（含分块）、其他走纯 chat

    结构化失败不改变任务结局：`parse_grading_output` 会退回自由文本，
    下载照旧，只是少了结构化清单。把用户的文档判失败从来不是一个好选项。
    md/pptx 转换不走这里（零 LLM，在 _run_processing 前段分流）。
    """
    if not task.is_grading:
        result = await chat(
            [{"role": "user", "content": prompt_template.replace("{{content}}", original_text)}],
            provider,
        )
        return _ModelOutput(result.content, None, (_MeteredCall(result, "chat"),))

    if _should_chunk(original_text):
        # thread_id 用 log_id：同一日志重跑命中同一时间线，已成功的块不再花钱
        graded = await grade_long_document(
            original_text,
            provider,
            prompt_template,
            thread_id=f"log-{log_id}",
            spec_lookup=spec_lookup,
        )
        grading = GradingResult(
            revised_text=graded.revised_text,
            issues=[GradingIssue(**i) for i in graded.issues],
            degraded=graded.degraded,
        )
        return _ModelOutput(
            grading.revised_text,
            # 各块引用的并集同样落进信封：长文档的溯源清单不能只在单发路径里有
            serialize_result(
                grading,
                refs=list(graded.refs) or None,
                note=graded.spec_note,
            ),
            # 命中缓存的块本轮没有外呼，再记账就是重复计成本
            tuple(
                _MeteredCall(
                    c.as_llm_result(provider.model_name),
                    # 用途里带上这块有没有真拿到清单，评测台才能把两类分开统计
                    "grading_degraded" if c.degraded else "grading",
                )
                for c in graded.calls
                if not c.cached
            ),
        )

    prompt = prompt_template.replace("{{content}}", original_text)
    spec = spec or _NO_SPEC
    result, payload = await chat_json(
        build_messages(prompt, spec.context),
        provider,
        json_schema=GRADING_JSON_SCHEMA,
    )
    grading = parse_grading_output(result.content, payload, original_text)
    return _ModelOutput(
        grading.revised_text,
        # refs/note 进信封：让"这次到底参考了哪几条规范"能从结果本身查出来
        serialize_result(grading, refs=spec.refs or None, note=spec.note),
        (
            _MeteredCall(
                result,
                "grading" if not grading.degraded else "grading_degraded",
            ),
        ),
    )


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

    done = log.status == LogStatus.DONE
    progress = None
    if log.status == LogStatus.PROCESSING:
        # 只在处理中读检查点：跑完之后这个字段没有意义，别为已结束的日志白开一次 sqlite
        snapshot = await read_progress(f"log-{log_id}")
        if snapshot is not None:
            progress = ChunkProgressResponse(total=snapshot.total, done=snapshot.done)

    # structured_output 为 NULL = 非批改任务/历史数据，必须继续透 None。
    # 若在这里退化成 []，前端会把"根本没批改"显示成"批改完成，未发现问题"（§3.7 的语义分界）
    graded = bool(done and log.structured_output)

    return ProcessResponse(
        log_id=log.id,
        status=log.status,
        processed_at=log.processed_at,
        error_message=log.error_message,
        processed_content=log.processed_content if done else None,
        issues=deserialize_issues(log.structured_output) if graded else None,
        grading_degraded=is_degraded(log.structured_output) if done else False,
        spec_refs=deserialize_refs(log.structured_output) if graded else None,
        spec_note=read_spec_note(log.structured_output) if graded else None,
        progress=progress,
        ocr_pending=bool(log.needs_ocr and log.status == LogStatus.PROCESSING),
        original_content=log.original_content if done else None,
    )


def _read_pptx_delivery(log_id: int) -> bytes | None:
    """读落盘的 PPT 交付件（同步，调用方丢线程池）；缺失返回 None 由路由转 400"""
    path = os.path.join(resolve_dir(settings.output_dir), "pptx", f"{log_id}.pptx")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _build_md_download(log: OperationLog, stem: str) -> tuple[io.BytesIO, str, str]:
    """
    拼 md 交付件（同步，调用方丢线程池）：有 MinerU 图片就打 zip，否则裸 .md。

    zip 内布局 {stem}.md + images/xxx，与 full.md 里的相对图片引用一致，
    解压后 md 渲染器能直接找到图。
    """
    md_text = log.processed_content or ""
    img_dir = os.path.join(resolve_dir(settings.output_dir), "mineru", str(log.id), "images")
    images: dict[str, bytes] = {}
    if os.path.isdir(img_dir):
        for name in sorted(os.listdir(img_dir)):
            path = os.path.join(img_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    images[name] = f.read()
    if images:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}.md", md_text)
            for name, data in images.items():
                zf.writestr(f"images/{name}", data)
        buf.seek(0)
        return buf, f"转换_{stem}.zip", "application/zip"
    return io.BytesIO(md_text.encode("utf-8")), f"转换_{stem}.md", "text/markdown; charset=utf-8"


@router.get("/download/{log_id}")
async def download(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """下载结果文档 —— 批改任务生成带红字标注的 diff，普通任务直接输出"""
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
    output_format = task.output_format if task and task.output_format else "docx"

    stem = os.path.splitext(log.original_filename or "文档")[0]

    # md 转换任务：交付 markdown；MinerU 路径带图时打 zip（md + images/，
    # 与 full.md 里的相对引用一致），无图直接给 .md
    if output_format == "md":
        buf, filename, media = await run_in_threadpool(_build_md_download, log, stem)
    elif output_format == "pptx":
        # PPT 交付物是处理阶段落盘的二进制（片3 版面修订后就地覆盖），下载直接读
        data = await run_in_threadpool(_read_pptx_delivery, log.id)
        if data is None:
            raise HTTPException(400, "PPT 交付文件缺失，请重新处理")
        buf = io.BytesIO(data)
        filename = f"转换_{stem}.pptx"
        media = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    else:
        # 结果永远是 .docx，所以文件名取原名去掉扩展再补 .docx——
        # 直接拼 original_filename 会让 PDF 输出一份叫 "xxx.pdf" 的 docx，下载方打不开
        if is_grading:
            # 引用清单从信封里取（阶段 6 A7）：批完那一刻召回了哪些条款已经定死，
            # 下载时回查规范库会因为库被删改而给出对不上的清单
            buf = create_diff_docx(
                log.original_content or "",
                log.processed_content or "",
                spec_refs=deserialize_refs(log.structured_output) or None,
            )
            filename = f"批改_{stem}.docx"
        else:
            buf = create_plain_docx(log.processed_content or "")
            filename = f"结果_{stem}.docx"
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # StreamingResponse 直接把 BytesIO 流转发给客户端，不需要落盘
    # 类比 Java 的 ResponseEntity<InputStreamResource> + Content-Disposition
    # 中文文件名要 RFC 5987 编码；filename* 是新版浏览器识别的
    from urllib.parse import quote

    disposition = (
        f'attachment; filename="result{os.path.splitext(filename)[1]}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    headers = {"Content-Disposition": disposition}

    return StreamingResponse(buf, media_type=media, headers=headers)
