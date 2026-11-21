"""PDF→PPT 忠实转换任务单测（output_format='pptx'，片1b）

覆盖四块：
- schema 互斥（pptx + 批改在创建与更新两条路都拦）；
- 上传 fail-fast（只收 PDF、必须有 MinerU token）；
- _run_ppt_conversion 的 MinerU→块渲染路径（假 parse，不出网）与失败路径；
- 零 LLM 边界 + 下载读落盘二进制 + 重试清掉历史大纲信封。

DB 走内存 SQLite（同 test_md_process.py），上传/输出目录指到 tmp_path。
"""

import asyncio
import io
import json

import httpx
import pymupdf
import pytest
from fastapi import HTTPException
from pptx import Presentation
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1 import process
from api.v1.tasks import update_task
from database import Base
from models.llm_call import LlmCall
from models.operation_log import LogStatus, OperationLog
from models.task import Task
from models.user import User
from schemas.task import TaskCreate, TaskUpdate
from services.mineru import MinerUError, MinerUResult

_PNG = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4)).tobytes("png")
_MD = "# 标题\n\n转换正文。\n\n![](images/a.jpg)"
# 页嵌套 content_list（真实 MinerU v2 形状），bbox 归一化 0..1000
_PAGES = [
    [
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "转换正文"}]},
            "bbox": [100, 100, 900, 200],
        }
    ]
]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    output = tmp_path / "outputs"
    monkeypatch.setattr(process.settings, "upload_dir", str(upload))
    monkeypatch.setattr(process.settings, "output_dir", str(output))
    return upload, output


def _user(db):
    u = User(username="ppttester", password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _task(db, user, output_format="pptx", is_grading=False):
    t = Task(
        user_id=user.id,
        name="转ppt",
        prompt_template="{{content}}",
        is_grading=is_grading,
        output_format=output_format,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _log(db, user, task, stored_name, *, original="原文预览", needs_ocr=False):
    log = OperationLog(
        user_id=user.id,
        task_id=task.id,
        task_name=task.name,
        original_filename=stored_name,
        stored_filename=stored_name,
        original_content=original,
        status=LogStatus.PROCESSING,
        needs_ocr=needs_ocr,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def _write(upload_dir, name, data):
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / name).write_bytes(data)


def _fake_mineru(monkeypatch, result=None, error=None):
    def fake(file_bytes, filename):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(process, "parse_pdf_via_mineru", fake)


def _result(**kw):
    base = dict(markdown=_MD, content_list=_PAGES, images={"a.jpg": _PNG}, page_sizes=[(595, 842)])
    base.update(kw)
    return MinerUResult(**base)


class _FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


# ---------- schema 互斥 ----------
def test_create_pptx_grading_rejected_at_schema():
    with pytest.raises(ValidationError, match="批改"):
        TaskCreate(name="转ppt", prompt_template="x", output_format="pptx", is_grading=True)


def test_update_to_pptx_on_grading_task_rejected(session):
    u = _user(session)
    t = _task(session, u, output_format="docx", is_grading=True)
    with pytest.raises(HTTPException, match="批改"):
        update_task(t.id, TaskUpdate(output_format="pptx"), db=session, current=u)


def test_update_to_grading_on_pptx_task_rejected(session):
    u = _user(session)
    t = _task(session, u)
    with pytest.raises(HTTPException, match="批改"):
        update_task(t.id, TaskUpdate(is_grading=True), db=session, current=u)


# ---------- 上传 fail-fast ----------
def test_upload_pptx_docx_rejected(session, dirs, monkeypatch):
    monkeypatch.setattr(process.settings, "mineru_token", "t")
    u = _user(session)
    t = _task(session, u)
    with pytest.raises(HTTPException, match=r"只支持 \.pdf"):
        asyncio.run(process.upload(t.id, _FakeUpload("a.docx", b"fake"), db=session, current=u))


def test_upload_pptx_pdf_without_token_rejected(session, dirs, monkeypatch):
    monkeypatch.setattr(process.settings, "mineru_token", "")
    u = _user(session)
    t = _task(session, u)
    with pytest.raises(HTTPException, match="MINERU_TOKEN"):
        asyncio.run(process.upload(t.id, _FakeUpload("a.pdf", b"%PDF-fake"), db=session, current=u))


# ---------- 转换成功路径 ----------
def test_ppt_conversion_done_zero_llm(session, dirs, monkeypatch):
    upload, output = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf", original="", needs_ocr=True)
    _fake_mineru(monkeypatch, result=_result())

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.DONE
    assert log.needs_ocr is False
    assert log.provider_name is None
    assert log.structured_output is None
    assert log.processed_content == _MD
    # 扫描件上传没有预览文本 → 归一化纯文本补底本
    assert log.original_content == "标题\n转换正文。"
    assert session.query(LlmCall).count() == 0

    # 交付二进制落盘且可被 python-pptx 重读，正文进了可编辑文本框
    pptx_path = output / "pptx" / f"{log.id}.pptx"
    prs = Presentation(io.BytesIO(pptx_path.read_bytes()))
    texts = [s.text_frame.text for slide in prs.slides for s in slide.shapes if s.has_text_frame]
    assert "转换正文" in texts

    # structured 留存旁路照旧（片2 grounding 要用）
    dest = output / "mineru" / str(log.id)
    assert (dest / "full.md").read_text(encoding="utf-8") == _MD
    assert json.loads((dest / "content_list.json").read_text(encoding="utf-8")) == _PAGES
    assert (dest / "images" / "a.jpg").read_bytes() == _PNG


def test_ppt_conversion_keeps_uploaded_preview(session, dirs, monkeypatch):
    """文字型 PDF 上传时已有提取预览，转换不覆盖 original_content"""
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf", original="上传时的预览")
    _fake_mineru(monkeypatch, result=_result())

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.DONE
    assert log.original_content == "上传时的预览"


def test_ppt_retry_clears_legacy_outline_envelope(session, dirs, monkeypatch):
    """阶段5老日志重试：旧 ppt_outline 信封必须清掉，不能混进转换结果"""
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    log.structured_output = json.dumps({"kind": "ppt_outline", "slides": []})
    session.commit()
    _fake_mineru(monkeypatch, result=_result())

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.DONE
    assert log.structured_output is None


# ---------- 失败路径 ----------
def test_ppt_mineru_error_failed(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    _fake_mineru(monkeypatch, error=MinerUError("页数超出配额"))

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "PDF 转 PPT 失败" in log.error_message
    assert "页数超出配额" in log.error_message


def test_ppt_transport_error_actionable_message(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    _fake_mineru(monkeypatch, error=httpx.ConnectError("connection refused"))

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "无法连接 MinerU 服务" in log.error_message


def test_ppt_empty_content_list_failed(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    _fake_mineru(monkeypatch, result=_result(content_list=[]))

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "识别结果为空" in log.error_message


def test_ppt_missing_stored_filename_failed(session, dirs):
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "a.pdf")
    log.stored_filename = None
    session.commit()

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "落盘记录缺失" in log.error_message


def test_ppt_missing_file_on_disk_failed(session, dirs):
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "ghost.pdf")

    asyncio.run(process._run_ppt_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "读取上传文件失败" in log.error_message


# ---------- 下载 ----------
def _done_ppt_log(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "报告.pdf", b"%PDF-fake")
    log = _log(session, u, t, "报告.pdf")
    _fake_mineru(monkeypatch, result=_result())
    asyncio.run(process._run_ppt_conversion(session, log))
    return u, log


def test_ppt_download_streams_stored_binary(session, dirs, monkeypatch):
    u, log = _done_ppt_log(session, dirs, monkeypatch)
    resp = asyncio.run(process.download(log.id, db=session, current=u))

    async def collect():
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(collect())
    assert resp.media_type.startswith(
        "application/vnd.openxmlformats-officedocument.presentationml"
    )
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    stored = (dirs[1] / "pptx" / f"{log.id}.pptx").read_bytes()
    assert body == stored
    assert len(list(Presentation(io.BytesIO(body)).slides)) >= 1


def test_ppt_download_missing_file_rejected(session, dirs, monkeypatch):
    u, log = _done_ppt_log(session, dirs, monkeypatch)
    (dirs[1] / "pptx" / f"{log.id}.pptx").unlink()
    with pytest.raises(HTTPException, match="交付文件缺失"):
        asyncio.run(process.download(log.id, db=session, current=u))
