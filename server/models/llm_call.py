"""LLM 调用计量 ORM —— 一次外呼记一行，供成本核算、重试排查和评测台回放。

【设计约束】
- 计量是旁路数据：写失败绝不能把业务流程带崩，所以写入方吞异常只告警。
- provider / model 存名称快照：提供商被删后这条记录仍然读得懂（同 operation_logs 思路）。
- 不存 prompt 和响应正文：体积大、可能含用户文档内容，评测要回放时另建样本表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---------- 归属 ----------
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # SET NULL：删提供商要能连带清掉引用，但计量行本身必须留下
    provider_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("model_providers.id", ondelete="SET NULL"), nullable=True
    )
    # 可为空：评测台、独立脚本发起的调用不挂在任何操作日志上
    log_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operation_logs.id", ondelete="SET NULL"), nullable=True
    )

    # ---------- 用途 ----------
    # chat=文档处理 / outline=大纲 / critic=视觉批评 / judge=评测打分 / embedding=向量化
    purpose: Mapped[str] = mapped_column(String(30), default="chat", nullable=False)

    # ---------- 快照 ----------
    provider_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ---------- 计量 ----------
    # 部分 OpenAI 兼容端点不返回 usage，缺省记 0 而不是 NULL，方便直接 SUM
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 从发起请求到收完响应，含排队等并发闸和重试退避的时间
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 含首次尝试，1 表示一次成功；大于 1 说明遇到过可重试的失败
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # ---------- 结果 ----------
    status: Mapped[str] = mapped_column(String(20), default="success", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<LlmCall id={self.id} purpose={self.purpose!r} "
            f"total_tokens={self.total_tokens} status={self.status!r}>"
        )
