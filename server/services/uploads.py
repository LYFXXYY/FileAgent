"""上传规模的上限 —— 字节数与 PDF 页数

为什么单独一个模块：上传有**两个入口**（`api/v1/process.py` 的工作文档、
`api/v1/specs.py` 的规范库），一个是 async 路由用 `UploadFile.read()`，一个是同步
路由用 `UploadFile.file.read()`。把"读多少算超"写两遍，迟早只改一边。

为什么默认就要有上限，而不是等上线再加：
- 上传是"先整份读进内存再提取"，没有上限时一个请求就能把 worker 内存吃光；
- PDF 还会整份传给 MinerU 云，而 MinerU **按页计费**——所以字节闸和页数闸是两件事：
  20 MB 里塞得下一份两千页的文件，字节没超、钱会超。
"""

from __future__ import annotations

import pymupdf
from fastapi import HTTPException, UploadFile

from config import get_settings

# 一次读 1 MB：足够让"超限即断"及时生效，又不会把大文件切成太多次系统调用
_CHUNK = 1024 * 1024


def _limit_bytes() -> int:
    """0 表示不限制（返回一个不可能达到的上限，让调用侧无需分支）"""
    mb = get_settings().max_upload_mb
    return mb * 1024 * 1024 if mb > 0 else 0


def _too_large() -> HTTPException:
    mb = get_settings().max_upload_mb
    return HTTPException(413, f"文件超过上限 {mb} MB，请拆分后再上传")


async def read_capped(file: UploadFile) -> bytes:
    """分块读取，超过上限立即拒绝——绝不先把整个文件吸进内存再判断"""
    limit = _limit_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        part = await file.read(_CHUNK)
        if not part:
            break
        total += len(part)
        if limit and total > limit:
            raise _too_large()
        chunks.append(part)
    return b"".join(chunks)


def read_capped_sync(file: UploadFile) -> bytes:
    """`read_capped` 的同步版，给 `def` 路由用（FastAPI 已把它丢进线程池）"""
    limit = _limit_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        part = file.file.read(_CHUNK)
        if not part:
            break
        total += len(part)
        if limit and total > limit:
            raise _too_large()
        chunks.append(part)
    return b"".join(chunks)


def check_pdf_pages(raw: bytes) -> None:
    """
    PDF 页数闸

    非 PDF 直接放过（docx 没有"页"的概念）。**打不开的也放过**：损坏件、带打开
    密码的加密件在提取层 `services/pdf.py` 各有专门的 400 文案，这里不抢判据、
    不把"加密"报成"页数超限"这种误导性的话。
    """
    max_pages = get_settings().max_pdf_pages
    if not max_pages or not raw.startswith(b"%PDF"):
        return
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        # PyMuPDF 对损坏件/加密件抛的异常类型跨版本变过，而且"为什么打不开"该由
        # services/pdf.py 定性（它有加密、无文字层等专门文案）。这里一律放过。
        return
    try:
        pages = doc.page_count
    finally:
        doc.close()
    if pages > max_pages:
        raise HTTPException(400, f"PDF 共 {pages} 页，超过上限 {max_pages} 页，请拆分后再上传")
