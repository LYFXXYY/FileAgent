"""规范文档相关 Pydantic 出入参。

阶段 4 只到落库，所以只有响应模型；输入走 multipart 文件 + 表单字段，没有 Create schema。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SpecDocResponse(BaseModel):
    """列表项与单条响应都用这个。chunks 字段不在这个响应里，避免列表页拉整篇正文"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    source_format: str
    char_count: int
    chunk_count: int
    uploaded_at: datetime


class SpecChunkResponse(BaseModel):
    """GET /specs/{id}/chunks 返回元素。带 text + 偏移，前端拿到就能拼回上下文"""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    text: str
    start: int
    end: int
