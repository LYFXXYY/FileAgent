"""
用户表 ORM 模型

【Java/JPA 对比】
- class User(Base)  ≈  @Entity public class User { ... }
- __tablename__     ≈  @Table(name = "users")
- Column(...)       ≈  @Column + @Id + @GeneratedValue
"""

from __future__ import annotations  # 让类型注解延迟解析，避免循环导入

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

# 仅类型检查时导入（运行时不执行），避免跨模块循环依赖
if TYPE_CHECKING:
    from models.operation_log import OperationLog
    from models.task import Task


class User(Base):
    """用户实体 —— 存账号和密码哈希，不存明文密码"""

    __tablename__ = "users"

    # INT PRIMARY KEY AUTO_INCREMENT
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # VARCHAR(50) UNIQUE NOT NULL
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)

    # bcrypt 哈希后的密码，固定 60 字节但预留 255（不同算法长度不同）
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # DATETIME DEFAULT CURRENT_TIMESTAMP
    # server_default=func.now() → 由 MySQL 的 DEFAULT NOW() 生成，不依赖应用层时钟
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )

    # ---------- 关系（类似 JPA @OneToMany） ----------
    # User.tasks → 该用户创建的所有 Task
    # back_populates="user" → 双向绑定，Task.user 能反向引用
    # cascade="all, delete-orphan" → 删用户时级联删其任务（类似 JPA CascadeType.ALL + orphanRemoval）
    # ⚠ Task 和 OperationLog 定义在其他文件中，from __future__ import annotations 处理了前向引用
    tasks: Mapped[list[Task]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    operation_logs: Mapped[list[OperationLog]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    # __repr__ 用于调试时打印对象，类似 Java 的 toString()
    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
