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

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import get_settings
from database import SessionLocal, get_db
from graphs.grading_graph import grade_long_document, read_progress
from graphs.ppt_graph import run_ppt_pipeline
from middleware.auth import get_current_user
from models.operation_log import LogStatus, OperationLog
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.grading import GRADING_JSON_SCHEMA, GradingIssue, GradingResult
from schemas.ppt_outline import PptOutline, deserialize_outline, serialize_outline
from schemas.process import ChunkProgressResponse, ProcessResponse, UploadResponse
from services.docx_md import docx_to_markdown
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
from services.pptx_builder import PptxError, build_pptx
from services.spec_retrieval import SpecContext, collect_spec_context
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

    # 3. 读取字节 + 按扩展名派发到对应的提取器
    file_bytes = await file.read()  # UploadFile.read() 是 async
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
    with open(os.path.join(_resolve_dir(settings.upload_dir), stored_name), "wb") as f:  # noqa: ASYNC230
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

    # 任务指定了 provider 时提前校验（防越权 + 防静默用错模型）：
    # 让问题在返回 processing 之前就以 400 暴露，而不是后台跑一半才失败
    if task.provider_id is not None:
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
            _mark_failed(db, log, "关联的任务已被删除")
            return

        # md 转换：零 LLM，在选 provider / OCR 兜底之前分流。
        # 扫描件 md 任务上传时也带 needs_ocr=True，由本分支自己走 MinerU，
        # 不进下面那条"OCR 后还要调 LLM"的批改兜底路径
        if task.output_format == "md":
            await _run_md_conversion(db, log)
            return

        # 扫描件兜底：正文还在 MinerU 云端没取回来，先 OCR 写回再走 LLM。
        # 放在选 provider 之前：OCR 失败时错误信息更直接（不用先查一圈模型）。
        # parse_pdf_via_mineru 是同步阻塞（含轮询 sleep），必须丢线程池
        if log.needs_ocr:
            if not log.stored_filename:
                _mark_failed(db, log, "扫描件落盘记录缺失，请重新上传")
                return
            stored_path = os.path.join(_resolve_dir(settings.upload_dir), log.stored_filename)
            try:
                with open(stored_path, "rb") as f:  # noqa: ASYNC230
                    pdf_bytes = f.read()
                result = await run_in_threadpool(
                    parse_pdf_via_mineru, pdf_bytes, log.original_filename
                )
            except (MinerUError, OSError) as e:
                _mark_failed(db, log, f"扫描件识别失败：{e}"[:2000])
                return
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                # 传输层异常（代理配置非法 / 连接中断等）文案对用户不可行动，
                # 归一成 MinerU 口径；细节进日志供排查
                logger.warning("MinerU 传输层异常 log_id=%s：%r", log.id, e)
                _mark_failed(db, log, "扫描件识别失败：无法连接 MinerU 服务，请检查网络后重试")
                return
            plain = markdown_to_plain_text(result.markdown)
            if not plain.strip():
                _mark_failed(db, log, "扫描件识别结果为空，请确认文档内容清晰可辨")
                return
            log.original_content = plain
            log.needs_ocr = False
            db.commit()
            # structured 留存是旁路：写盘失败只告警，不把已拿到的正文判成失败
            save_structured(
                result,
                os.path.join(_resolve_dir(settings.output_dir), "mineru", str(log.id)),
            )

        # 选 provider：任务指定 > 当前用户的默认 active（按 text 能力筛）
        try:
            provider = resolve_provider(db, log.user_id, task.provider_id, "text")
        except LLMError as e:
            _mark_failed(db, log, str(e)[:2000])
            return

        # 快照成普通值：计量写入要丢线程池，不能跨线程用这个 session 的 ORM 对象
        provider_ref = (provider.id, provider.name)

        # PPT 任务的批评环节需要 vision 提供商。没配也不报错——s5.6 设计允许跳过
        # 批评直接交付，至少大纲+PPTX 仍然可用
        critic_provider = None
        critic_provider_ref: tuple[int, str] | None = None
        if task.output_format == "pptx":
            try:
                critic_provider = resolve_provider(db, log.user_id, None, "vision")
                critic_provider_ref = (critic_provider.id, critic_provider.name)
            except LLMError:
                logger.info("用户 %s 没有 vision 提供商，PPT 批评循环跳过", log.user_id)

        original_text = log.original_content or ""

        # 规范条款召回（阶段 6）。只在"批改 + 非 PPT"时尝试，且两条路径分开：
        # - 单发：整篇查一次，条款进那一条 user 消息
        # - 分块：按块查（长文档各段涉及的条款不同，全局 top-k 等于只按开头检索），
        #   查询能力以闭包传进图，节点自己不碰 DB
        # 检索失败不把已能跑通的批改判成失败：collect_spec_context 内部兜住并回传 note
        spec = _NO_SPEC
        spec_lookup = None
        if task.is_grading and task.output_format != "pptx":
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
                critic_provider=critic_provider,
                spec=spec,
                spec_lookup=spec_lookup,
            )
        except LLMError as e:
            await _meter(
                log,
                provider_ref,
                purpose=_error_purpose_for(task),
                error=str(e),
                attempts=e.attempts,
            )
            _mark_failed(db, log, str(e)[:2000])
            return

        for call in output.calls:
            # 批评外呼归 critic 提供商，其他归文本/大纲提供商
            ref = (
                critic_provider_ref
                if critic_provider_ref and call.purpose.startswith("critic")
                else provider_ref
            )
            await _meter(log, ref, purpose=call.purpose, result=call.result)

        log.processed_content = output.revised_text
        log.structured_output = output.structured
        # 记实际选中的 provider 名，不再事后回查默认提供商（回查会记错）
        log.provider_name = provider_ref[1]
        log.status = LogStatus.DONE
        log.processed_at = func.now()
        db.commit()
        # 赋的是 SQL 表达式，commit 后 refresh 才能拿到 DB 时钟的真实值
        db.refresh(log)
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
    # 时间列全站由 DB 时钟生成（uploaded_at 是 func.now()）。这里若用应用侧 now()，
    # 容器与宿主机的钟差会让 processed_at 反而早于 uploaded_at，耗时算出负数
    log.processed_at = func.now()
    db.commit()
    db.refresh(log)


async def _run_md_conversion(db: Session, log: OperationLog) -> None:
    """
    md 转换任务的后台处理：docx 走本地转换（免费），PDF 一律走 MinerU。

    零 LLM：不选 provider、不写 llm_calls（同 MinerU/embedding 的成本边界）。
    processed_content 存 markdown 原文（含表格与图片引用）；PDF 路径同时把
    归一化纯文本写回 original_content，前端预览与批改路径同源。
    """
    if not log.stored_filename:
        _mark_failed(db, log, "文件落盘记录缺失，请重新上传")
        return
    stored_path = os.path.join(_resolve_dir(settings.upload_dir), log.stored_filename)
    try:
        with open(stored_path, "rb") as f:  # noqa: ASYNC230
            file_bytes = f.read()
    except OSError as e:
        _mark_failed(db, log, f"读取上传文件失败：{e}")
        return

    ext = os.path.splitext(log.stored_filename)[1].lower()
    if ext == ".docx":
        try:
            markdown = await run_in_threadpool(docx_to_markdown, file_bytes)
        except Exception as e:
            _mark_failed(db, log, f"Word 转 Markdown 失败：{e}"[:2000])
            return
        if not markdown.strip():
            _mark_failed(db, log, "文档没有可转换的正文内容")
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
            _mark_failed(db, log, f"PDF 转 Markdown 失败：{e}"[:2000])
            return
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            logger.warning("MinerU 传输层异常 log_id=%s：%r", log.id, e)
            _mark_failed(db, log, "PDF 转 Markdown 失败：无法连接 MinerU 服务，请检查网络后重试")
            return
        if not result.markdown.strip():
            _mark_failed(db, log, "识别结果为空，请确认文档内容清晰可辨")
            return
        log.processed_content = result.markdown
        log.original_content = markdown_to_plain_text(result.markdown)
        # structured 留存是旁路（图片给下载 zip 与后续 PPT 带图用），写盘失败只告警
        save_structured(
            result,
            os.path.join(_resolve_dir(settings.output_dir), "mineru", str(log.id)),
        )

    log.needs_ocr = False
    log.status = LogStatus.DONE
    log.processed_at = func.now()
    db.commit()
    db.refresh(log)


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
    if task.output_format == "pptx":
        return "outline"
    return "grading" if task.is_grading else "chat"


def _should_chunk(original_text: str) -> bool:
    """
    只有批改类才分块（调用方已判过 is_grading），且长到超预算

    摘要、翻译、润色不分块：它们的产出需要对**整篇**负责，逐块做完再拼只会得到
    N 份局部摘要，比一次性塞进去更差。上限不够就报错，也不假装能分块。
    """
    budget = settings.longdoc_chunk_chars
    return bool(settings.longdoc_enabled) and len(original_text) > budget


def _render_outline_as_text(outline: PptOutline) -> str:
    """
    s5.2 的过渡产物：把大纲渲染成可读纯文本塞进 processed_content

    真实交付是 s5.3 的 PPTX 二进制。这条文本只是让下载路由在 s5.3 之前不至于空响应，
    也方便单测断言"至少能看到大纲内容"。
    """
    if not outline.slides:
        return "（大纲为空，未生成任何幻灯片页）"
    parts = []
    if outline.title:
        parts.append(outline.title)
        parts.append("")
    for i, slide in enumerate(outline.slides, start=1):
        parts.append(f"第 {i} 页：{slide.title}")
        for b in slide.bullets:
            parts.append(f"  - {b}")
    if outline.degraded:
        parts.append("")
        parts.append("（注意：模型未按大纲契约输出，已退化为按行切页；可在评测台核查）")
    return "\n".join(parts)


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
    critic_provider: ModelProvider | None = None,
    spec: SpecContext | None = None,
    spec_lookup=None,
) -> _ModelOutput:
    """
    三条路径：pptx 走 PPT 流水线（含批评循环）、批改走结构化（含分块）、其他走纯 chat

    结构化失败不改变任务结局：`parse_grading_output` / `parse_outline_output` 都会
    退回自由文本，下载照旧，只是少了结构化清单 / 大纲契约。把用户的文档判失败从来不是
    一个好选项。

    `critic_provider=None` 时 PPT 流水线跳过批评，s5.2 行为保持不变；
    PPT 调用方传了 critic_provider 才会进入 s5.6 的批评循环。
    """
    if task.output_format == "pptx":
        run = await run_ppt_pipeline(
            original_text,
            provider,
            prompt_template,
            thread_id=f"log-{log_id}",
            critic_provider=critic_provider,
        )
        plain = _render_outline_as_text(run.outline)
        # 每笔外呼转成 _MeteredCall；purpose 由 PptCall 自带（区分 critic / outline / 降级）
        calls = tuple(_MeteredCall(c.result, c.purpose) for c in run.calls)
        return _ModelOutput(plain, serialize_outline(run.outline), calls)

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


def _build_md_download(log: OperationLog, stem: str) -> tuple[io.BytesIO, str, str]:
    """
    拼 md 交付件（同步，调用方丢线程池）：有 MinerU 图片就打 zip，否则裸 .md。

    zip 内布局 {stem}.md + images/xxx，与 full.md 里的相对图片引用一致，
    解压后 md 渲染器能直接找到图。
    """
    md_text = log.processed_content or ""
    img_dir = os.path.join(_resolve_dir(settings.output_dir), "mineru", str(log.id), "images")
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
        # PPT 任务走 pptx 二进制；structured_output 必须是 ppt_outline 形态才能拼
        outline = deserialize_outline(log.structured_output)
        if outline is None:
            raise HTTPException(400, "PPT 大纲缺失或不是 ppt_outline 形态，无法拼装")
        try:
            buf = io.BytesIO(
                build_pptx(
                    outline,
                    image_refs=None,  # s5.3 文字-only；图片轮启用时这里换成图字典
                )
            )
        except PptxError as e:
            raise HTTPException(400, f"PPTX 拼装失败：{e}") from e
        filename = f"演示_{stem}.pptx"
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
