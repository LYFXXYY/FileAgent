"""
模型提供商 ORM —— 存 LLM 的 API 地址、密钥、模型名称

【设计思路】
- 所有模型（豆包、通义千问、GPT 等）统一用 OpenAI 兼容接口
- 换模型只需要在网页上改配置，不用动代码
- 每条记录 = 一整套模型参数（地址、key、模型名、token 上限、温度）
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.task import Task


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 外键 → users.id。每个提供商归属一个用户，CRUD 全部按 user_id 隔离
    # （和 Task 一致），避免用户之间互相看到/改写/删除对方的 API Key
    # ondelete=CASCADE：用户被删除时其提供商一并清理
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 自定义名称，如 "豆包-pro" / "GPT-4o"
    name: Mapped[str] = mapped_column(String(50), nullable=False)

    # API 基础地址，如 https://ark.cn-beijing.volces.com/api/v3
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)

    # API Key —— 生产环境应加密存储（后续可加字段级加密）
    api_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # 模型标识符，如 doubao-pro-128k / gpt-4o
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 能力标记，逗号分隔：text / vision / embedding
    # 一个模型通常只主打一种能力；按用途筛提供商，避免拿纯文本模型去处理图片
    capabilities: Mapped[str] = mapped_column(String(100), default="text", nullable=False)

    # 单次请求最大输出 token 数，默认 4096
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096, nullable=False)

    # 采样温度 0~2，越高越发散、越低越保守
    temperature: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)

    # 软启用/禁用 —— False 时不出现在任务下拉框里，但不删除记录（可恢复）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # ON UPDATE CURRENT_TIMESTAMP：MySQL 原生行为，ORM 层不做额外处理
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ---------- 关系 ----------
    # provider.tasks → 使用该提供商的所有任务
    tasks: Mapped[list[Task]] = relationship(back_populates="provider")

    def __repr__(self) -> str:
        return f"<ModelProvider id={self.id} name={self.name!r} model={self.model_name!r}>"
