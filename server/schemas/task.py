"""任务相关的请求/响应模型"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 与 models/task.py 的 output_format 列取值集合一致。加新格式时同步改
OUTPUT_FORMATS = ("docx", "pptx", "md")
OutputFormat = Literal["docx", "pptx", "md"]


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    prompt_template: str = Field(min_length=1)
    is_grading: bool = False
    provider_id: int | None = None
    # 默认 docx 是为了让阶段 0–4 期间建的任务行为不变；前端表单会显式带值
    output_format: OutputFormat = "docx"

    @model_validator(mode="after")
    def _md_not_grading(self):
        # md 转换零 LLM、产物是 markdown，没有修订文可供 diff 标红
        if self.output_format == "md" and self.is_grading:
            raise ValueError("md 转换任务不能同时是批改类任务")
        return self


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    prompt_template: str | None = None
    is_grading: bool | None = None
    provider_id: int | None = None
    output_format: OutputFormat | None = None


class TaskResponse(BaseModel):
    id: int
    user_id: int
    provider_id: int | None
    name: str
    description: str | None
    prompt_template: str
    is_grading: bool
    output_format: OutputFormat
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
