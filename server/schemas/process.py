"""上传/处理/下载 相关的响应模型"""

from datetime import datetime

from pydantic import BaseModel

from models.operation_log import LogStatus
from schemas.grading import GradingIssue


class UploadResponse(BaseModel):
    """上传接口返回：告知前端 log_id，之后调 /process/{id}

    extracted_text 返回提取的纯文本，供前端在处理步骤预览确认
    """

    log_id: int
    original_filename: str
    content_length: int
    extracted_text: str
    status: LogStatus
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class ChunkProgressResponse(BaseModel):
    """长文档分块批改的进度快照

    只有分块路径才有值：读的是 LangGraph 检查点，`plan` 节点跑完之前拿不到块数，
    那种情况下字段是 None 而不是 0/0——没有依据的数字比没有数字更坏。
    """

    total: int
    done: int


class ProcessResponse(BaseModel):
    """处理完成后的结果

    processed_content 在 status=done 时返回 LLM 处理结果文本，供前端在线预览
    """

    log_id: int
    status: LogStatus
    processed_at: datetime | None
    error_message: str | None
    processed_content: str | None = None
    # 结构化批改的批注清单。None = 非批改任务或历史数据；空列表 = 批改了但没报问题
    issues: list[GradingIssue] | None = None
    # true = 是批改任务但模型没按格式出 JSON，已退回自由文本。
    # 丢了它，前端只能显示"0 处问题"，等于把失败伪装成没问题
    grading_degraded: bool = False
    # 分块进度。非分块任务（短文、摘要类）恒为 None
    progress: ChunkProgressResponse | None = None

    model_config = {"from_attributes": True}
