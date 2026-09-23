"""
操作日志 ORM —— 记录每一次 Word 上传 → LLM 处理 → 下载的全链路

【状态流转】
  pending → processing → done / failed
  每个状态变更都要记录 processed_at（处理完成时刻），方便看耗时

【字段说明】
  original_filename  → 用户上传时的原始文件名（存到磁盘上文件名可能变）
  original_content   → 上传时 python-docx 提取的纯文本（保存以便 diff 对比）
  processed_content  → LLM 返回的结果文本
  error_message      → 失败时存具体错误信息（如超时、API Key 无效）
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.task import Task
    from models.user import User


# ---------- 状态枚举 ----------
# Python Enum → SQLAlchemy Enum → MySQL ENUM('pending','processing','done','failed')
# 类比 Java 的 enum + @Enumerated(EnumType.STRING)
class LogStatus(str, enum.Enum):
    PENDING = "pending"  # 已上传，等待处理
    PROCESSING = "processing"  # 正在调 LLM
    DONE = "done"  # 处理完成，可下载
    FAILED = "failed"  # 处理失败，看 error_message


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---------- 外键 ----------
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=False
    )
    # 关联的任务（可为 NULL：支持任务被删除后日志仍保留）
    # ondelete=SET NULL：任务被删除时自动置空，日志靠 task_name 快照仍可读
    task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    # ---------- 任务快照 ----------
    # 存 task_name / provider_name 是为了任务被改名或删掉后日志仍可读
    task_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # ---------- 文件信息 ----------
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # ---------- 正文内容 ----------
    # Text 在 MySQL 中映射为 TEXT（65KB）。Word 文档纯文本很少超此限制
    # 如果有大文件需求，可改为 sqlalchemy.dialects.mysql.LONGTEXT（4GB）
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---------- 时间戳 ----------
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------- 状态 ----------
    # values_callable → 让 SQLAlchemy 存枚举的 value（"pending"）而非 name（"PENDING"）
    status: Mapped[LogStatus] = mapped_column(
        Enum(LogStatus, values_callable=lambda x: [e.value for e in x]),
        default=LogStatus.PENDING,
        nullable=False,
    )

    # 失败时存具体的异常信息，如 LLM 超时、API Key 无效等
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---------- 关系 ----------
    user: Mapped[User] = relationship(back_populates="operation_logs")
    task: Mapped[Task | None] = relationship(back_populates="operation_logs")

    def __repr__(self) -> str:
        return (
            f"<OperationLog id={self.id} filename={self.original_filename!r} status={self.status}>"
        )
