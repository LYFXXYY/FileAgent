"""
全局配置模块 —— 读取 .env 环境变量

【Java 对比】
- 类似 Spring Boot 的 @ConfigurationProperties + application.yml
- 但比 YAML 更安全：Pydantic 启动时就校验字段，缺必填项直接报错，不留到运行时才 NPE
- 所有 secret 都从环境变量读（不进 git），和 Spring 的 Vault / 配置中心思路一致
"""

import os
from pydantic_settings import BaseSettings

# __file__ 是当前 config.py 的绝对路径
# dirname(__file__) 就是 server/ 目录，.env 在这个目录下
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")


class Settings(BaseSettings):
    """
    配置类 —— 字段名和 .env 的 key 对应（大小写不敏感）

    Pydantic 会自动：
    1. 从环境变量读取同名 key（如 DATABASE_URL）
    2. 类型转换（str → int 等）
    3. 缺失必填字段时启动直接报错（fail-fast 原则）
    """

    # ---------- MySQL 连接 ----------
    # SQLAlchemy + PyMySQL 的连接字符串格式：
    #   mysql+pymysql://user:password@host:port/database
    # 类似 JDBC 的 jdbc:mysql://user:pass@host:port/db
    database_url: str

    # ---------- JWT 认证 ----------
    # 用于签名 access_token 的密钥
    jwt_secret_key: str
    # token 过期时间（小时）—— 短期 token 方案，不用 Redis
    jwt_expire_hours: int = 2

    # ---------- 文件存储 ----------
    # 上传/输出目录（相对于 server/ 的路径，也可写绝对路径）
    upload_dir: str = "uploads"
    output_dir: str = "outputs"

    class Config:
        env_file = ENV_FILE       # 指定 .env 文件路径
        env_file_encoding = "utf-8"


# 模块级单例 —— 整个应用启动时只加载一次配置
# 类似 Spring 的 @Component 单例 bean
# 其他模块通过 from config import get_settings 获取
_settings: Settings | None = None


def get_settings() -> Settings:
    """返回缓存的配置单例（依赖注入的源头，FastAPI 的 Depends 最终调这里）"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
