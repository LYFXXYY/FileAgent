"""操作日志相关的响应模型"""

from datetime import datetime

from pydantic import BaseModel

from models.operation_log import LogStatus


class LogResponse(BaseModel):
    """日志条目（不返回 original_content/processed_content 大字段，节省带宽）"""

    id: int
    user_id: int
    task_id: int | None
    task_name: str
    provider_name: str | None
    original_filename: str
    uploaded_at: datetime
    processed_at: datetime | None
    status: LogStatus
    error_message: str | None

    model_config = {"from_attributes": True}


class LogListResponse(BaseModel):
    """分页响应"""

    total: int
    page: int
    page_size: int
    items: list[LogResponse]
