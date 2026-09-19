"""任务相关的请求/响应模型"""

from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    prompt_template: str = Field(min_length=1)
    is_grading: bool = False
    provider_id: int | None = None


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    prompt_template: str | None = None
    is_grading: bool | None = None
    provider_id: int | None = None


class TaskResponse(BaseModel):
    id: int
    user_id: int
    provider_id: int | None
    name: str
    description: str | None
    prompt_template: str
    is_grading: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
