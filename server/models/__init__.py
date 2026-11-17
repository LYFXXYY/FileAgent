"""
ORM 模型包 —— 导入所有模型，让 create_all() 能发现它们

【Java 对比】
- 类似 Hibernate 的 hibernate.packagesToScan 配置
- 这里通过显式 import 让 SQLAlchemy 的 metadata 收集到所有表定义
"""

# ⚠ 导入顺序 = 外键依赖顺序（被引用的表先定义，引用的表后定义）
# 这样 SQLAlchemy 才能解析外键字符串（如 ForeignKey("users.id")）
from models.llm_call import LlmCall
from models.operation_log import OperationLog
from models.provider import ModelProvider
from models.spec_doc import SpecChunk, SpecDoc
from models.task import Task
from models.user import User

# __all__ 声明：当外部 from models import * 时，只导出这些名字
# 类似 Java 的 public 接口 vs 内部实现
__all__ = [
    "LlmCall",
    "ModelProvider",
    "OperationLog",
    "SpecChunk",
    "SpecDoc",
    "Task",
    "User",
]
