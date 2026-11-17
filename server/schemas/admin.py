"""管理端（/admin/*）的请求/响应模型（SPEC §9.11 简单版）"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# 角色取值。加新角色（如 auditor）时改这里 + require_admin 的判定
ROLES = ("user", "admin")


class AdminUserRow(BaseModel):
    """用户管理列表的一行。用量字段是即时聚合，不入库"""

    id: int
    username: str
    role: str
    can_use_default: bool
    created_at: datetime
    task_count: int
    log_count: int
    total_tokens: int

    model_config = {"from_attributes": True}


class RoleUpdate(BaseModel):
    role: Literal["user", "admin"]


class DefaultAccessUpdate(BaseModel):
    can_use_default: bool
