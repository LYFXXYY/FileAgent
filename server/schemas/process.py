"""上传/处理/下载 相关的响应模型"""

from datetime import datetime

from pydantic import BaseModel

from models.operation_log import LogStatus


class UploadResponse(BaseModel):
    """上传接口返回：告知前端 log_id，之后调 /process/{id}"""
    log_id: int
    original_filename: str
    content_length: int
    status: LogStatus
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class ProcessResponse(BaseModel):
    """处理完成后的结果"""
    log_id: int
    status: LogStatus
    processed_at: datetime | None
    error_message: str | None

    model_config = {"from_attributes": True}
