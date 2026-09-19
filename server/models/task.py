"""
任务 ORM —— 用户自定义的 Prompt 模板

【核心概念】
- Task = 一个处理流程的定义：名字 + Prompt 模板 + 是否批改
- Prompt 模板中用 {{content}} 作为占位符，执行时替换为 Word 文档的文本内容
- 一个用户有多个任务，一个任务可以指定不同的 LLM 提供商
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.operation_log import OperationLog
    from models.provider import ModelProvider
    from models.user import User


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 外键 → users.id。创建时填 user_id，查询时可通过 task.user 拿到对象
    # index=True → 创建索引，加速 WHERE user_id = ? 查询
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=False
    )

    # 外键 → model_providers.id。可为 NULL：不选 provider 时由系统决定用哪个
    provider_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("model_providers.id"), nullable=True
    )

    # 任务名称，如 "作文批改""文本润色"
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 任务描述（可选，纯文本，给用户看的）
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Prompt 模板，TEXT 类型存长文本
    # 模板中 {{content}} 会被替换为文档内容，如：
    #   "请批改以下作文，标出语法错误和用词不当之处：\n\n{{content}}"
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)

    # 是否为批改类任务 —— True 时下载会做 diff 标红
    is_grading: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ---------- 关系 ----------
    # back_populates 声明双向关系：Task.user ↔ User.tasks
    user: Mapped[User] = relationship(back_populates="tasks")
    provider: Mapped[ModelProvider | None] = relationship(back_populates="tasks")

    # 一次操作日志关联一个任务
    operation_logs: Mapped[list[OperationLog]] = relationship(back_populates="task")

    def __repr__(self) -> str:
        return f"<Task id={self.id} name={self.name!r} user_id={self.user_id}>"
