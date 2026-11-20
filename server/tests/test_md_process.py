"""md 转换任务单测（① output_format='md'）

覆盖四块：
- schema 互斥（md + 批改在创建与更新两条路都拦）；
- _run_md_conversion 的 docx 本地路径与 PDF MinerU 路径（假 parse，不出网）；
- _build_md_download 的裸 md / 带图 zip 两种交付形态；
- 零 LLM 边界：全程不写 llm_calls。

DB 走内存 SQLite（同 test_admin.py），上传/输出目录指到 tmp_path。
"""

import asyncio
import io
import json
import os
import zipfile

import httpx
import pytest
from docx import Document
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1 import process
from api.v1.tasks import create_task, update_task
from database import Base
from models.llm_call import LlmCall
from models.operation_log import LogStatus, OperationLog
from models.task import Task
from models.user import User
from schemas.task import TaskCreate, TaskUpdate
from services.mineru import MinerUError, MinerUResult
from services.ocr import markdown_to_plain_text


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
    """把 process 模块的上传/输出目录指到临时目录，避免碰真实 uploads/outputs"""
    upload = tmp_path / "uploads"
    output = tmp_path / "outputs"
    monkeypatch.setattr(process.settings, "upload_dir", str(upload))
    monkeypatch.setattr(process.settings, "output_dir", str(output))
    return upload, output


def _user(db):
    u = User(username="mdtester", password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _task(db, user, output_format="md", is_grading=False):
    t = Task(
        user_id=user.id,
        name="转md",
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


def _docx_bytes(text="正文段落", heading=None):
    doc = Document()
    if heading:
        doc.add_heading(heading, level=1)
    if text:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _write(upload_dir, name, data):
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / name).write_bytes(data)


# ---------- schema 互斥：创建与更新 ----------
def test_create_md_task_ok(session):
    u = _user(session)
    body = TaskCreate(name="转md", prompt_template="x", output_format="md")
    row = create_task(body, db=session, current=u)
    assert row.output_format == "md"


def test_create_md_grading_rejected_at_schema():
    """互斥在 pydantic 层就拦下，走不到路由"""
    with pytest.raises(ValidationError, match="批改"):
        TaskCreate(name="转md", prompt_template="x", output_format="md", is_grading=True)


def test_update_to_md_on_grading_task_rejected(session):
    """批改任务单改 output_format 也要拦：合并后的值非法"""
    u = _user(session)
    t = _task(session, u, output_format="docx", is_grading=True)
    with pytest.raises(HTTPException, match="批改"):
        update_task(t.id, TaskUpdate(output_format="md"), db=session, current=u)


def test_update_to_grading_on_md_task_rejected(session):
    u = _user(session)
    t = _task(session, u, output_format="md")
    with pytest.raises(HTTPException, match="批改"):
        update_task(t.id, TaskUpdate(is_grading=True), db=session, current=u)


def test_update_md_task_other_fields_ok(session):
    u = _user(session)
    t = _task(session, u, output_format="md")
    row = update_task(t.id, TaskUpdate(name="改名"), db=session, current=u)
    assert row.name == "改名"
    assert row.output_format == "md"


# ---------- 上传 fail-fast：md + PDF 必须有 token ----------
class _FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


def test_upload_md_pdf_without_token_rejected(session, dirs, monkeypatch):
    monkeypatch.setattr(process.settings, "mineru_token", "")
    u = _user(session)
    t = _task(session, u)
    with pytest.raises(HTTPException, match="MINERU_TOKEN"):
        asyncio.run(process.upload(t.id, _FakeUpload("a.pdf", b"%PDF-fake"), db=session, current=u))


def test_upload_md_docx_without_token_ok(session, dirs, monkeypatch):
    """docx 走本地转换，不受 token 缺失限制"""
    monkeypatch.setattr(process.settings, "mineru_token", "")
    u = _user(session)
    t = _task(session, u)
    resp = asyncio.run(
        process.upload(t.id, _FakeUpload("a.docx", _docx_bytes()), db=session, current=u)
    )
    assert resp.ocr_pending is False
    assert resp.content_length > 0


# ---------- docx 本地路径 ----------
def test_docx_conversion_done(session, dirs):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.docx", _docx_bytes("这是正文。", heading="第一章"))
    log = _log(session, u, t, "a.docx")

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.DONE
    assert "# 第一章" in log.processed_content
    assert "这是正文。" in log.processed_content
    # docx 路径不动 original_content（上传时已提取好）
    assert log.original_content == "原文预览"
    assert log.processed_at is not None
    assert session.query(LlmCall).count() == 0


def test_docx_empty_document_failed(session, dirs):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "empty.docx", _docx_bytes(text=""))
    log = _log(session, u, t, "empty.docx")

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "没有可转换的正文" in log.error_message


def test_docx_corrupt_bytes_failed(session, dirs):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "bad.docx", b"not-a-docx")
    log = _log(session, u, t, "bad.docx")

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "Word 转 Markdown 失败" in log.error_message


# ---------- PDF MinerU 路径 ----------
_MD = "# 标题\n\n正文第一段。\n\n![](images/a.jpg)"


def _fake_mineru(monkeypatch, result=None, error=None):
    def fake(file_bytes, filename):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(process, "parse_pdf_via_mineru", fake)


def test_pdf_conversion_done_and_structured_saved(session, dirs, monkeypatch):
    upload, output = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf", original="", needs_ocr=True)
    result = MinerUResult(
        markdown=_MD,
        content_list=[{"type": "text", "text": "正文第一段。"}],
        images={"a.jpg": b"\xff\xd8fake"},
    )
    _fake_mineru(monkeypatch, result=result)

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.DONE
    assert log.needs_ocr is False
    assert log.processed_content == _MD
    # 纯文本回填：图片引用不进 original_content（与批改路径同源约定）
    assert log.original_content == markdown_to_plain_text(_MD)
    assert "![" not in log.original_content
    assert session.query(LlmCall).count() == 0

    dest = output / "mineru" / str(log.id)
    assert (dest / "full.md").read_text(encoding="utf-8") == _MD
    assert json.loads((dest / "content_list.json").read_text(encoding="utf-8")) == (
        result.content_list
    )
    assert (dest / "images" / "a.jpg").read_bytes() == b"\xff\xd8fake"


def test_pdf_mineru_error_failed(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    _fake_mineru(monkeypatch, error=MinerUError("页数超出配额"))

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "PDF 转 Markdown 失败" in log.error_message
    assert "页数超出配额" in log.error_message


def test_pdf_transport_error_actionable_message(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    _fake_mineru(monkeypatch, error=httpx.ConnectError("connection refused"))

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "无法连接 MinerU 服务" in log.error_message


def test_pdf_empty_markdown_failed(session, dirs, monkeypatch):
    upload, _ = dirs
    u = _user(session)
    t = _task(session, u)
    _write(upload, "a.pdf", b"%PDF-fake")
    log = _log(session, u, t, "a.pdf")
    _fake_mineru(monkeypatch, result=MinerUResult(markdown="  \n"))

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "识别结果为空" in log.error_message


# ---------- 公共失败路径 ----------
def test_missing_stored_filename_failed(session, dirs):
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "a.docx")
    log.stored_filename = None
    session.commit()

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "落盘记录缺失" in log.error_message


def test_missing_file_on_disk_failed(session, dirs):
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "ghost.docx")

    asyncio.run(process._run_md_conversion(session, log))

    assert log.status == LogStatus.FAILED
    assert "读取上传文件失败" in log.error_message


# ---------- 下载交付形态 ----------
def test_md_download_plain_without_images(session, dirs):
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "报告.docx")
    log.processed_content = "# 标题\n\n正文"
    log.status = LogStatus.DONE
    session.commit()

    buf, filename, media = process._build_md_download(log, "报告")

    assert filename == "转换_报告.md"
    assert media.startswith("text/markdown")
    assert buf.read() == "# 标题\n\n正文".encode()


def test_md_download_zip_with_images(session, dirs):
    _, output = dirs
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "报告.pdf")
    log.processed_content = "# 标题\n\n![](images/a.jpg)"
    log.status = LogStatus.DONE
    session.commit()
    img_dir = output / "mineru" / str(log.id) / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "a.jpg").write_bytes(b"\xff\xd8fake")

    buf, filename, media = process._build_md_download(log, "报告")

    assert filename == "转换_报告.zip"
    assert media == "application/zip"
    with zipfile.ZipFile(buf) as zf:
        assert set(zf.namelist()) == {"报告.md", "images/a.jpg"}
        assert zf.read("报告.md").decode() == log.processed_content
        assert zf.read("images/a.jpg") == b"\xff\xd8fake"


def test_md_download_ignores_non_image_dirs(session, dirs):
    """structured 目录里只有 images/ 下的文件进 zip，full.md 等留存不重复打包"""
    _, output = dirs
    u = _user(session)
    t = _task(session, u)
    log = _log(session, u, t, "报告.pdf")
    log.processed_content = "正文"
    session.commit()
    dest = output / "mineru" / str(log.id)
    dest.mkdir(parents=True)
    (dest / "full.md").write_text("留存副本", encoding="utf-8")

    _, filename, _ = process._build_md_download(log, "报告")
    assert filename.endswith(".md")
    assert os.path.isdir(dest)  # 留存不受下载影响
