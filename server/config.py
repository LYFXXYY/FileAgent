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

    # ---------- 跨域 ----------
    # 允许的源，逗号分隔多个
    # 必须在 Settings 里声明：pydantic-settings 只把 .env 的值喂给本对象，
    # 不会写入 os.environ。用 os.getenv("CORS_ORIGINS") 读 .env 永远读到 None
    cors_origins: str = "http://localhost:5173"

    # ---------- LLM 调用 ----------
    # 连接阶段超时（秒）：只管 TCP + TLS 建连，与模型生成快慢无关
    llm_timeout_connect: float = 10.0
    # 读取阶段超时（秒）：等响应收完。长文档、批改类任务要放宽
    llm_timeout_read: float = 120.0
    # 重试次数（0 = 不重试）。只对网络错误 / 超时 / 429 / 5xx 生效，其他 4xx 不重试
    llm_max_retries: int = 2
    # 进程内并发闸：同时在飞的 LLM 请求数。多 worker 部署下每个进程各算各的
    llm_max_concurrency: int = 4
    # 是否让 httpx 读系统代理环境变量。访问需要代理的境外端点时要 True；
    # 但 NO_PROXY 里若有非法写法（如裸 [::1]）会导致客户端构造直接报错，此时设 False
    llm_trust_env_proxy: bool = True

    # ---------- 长文档分块批改（阶段 3）----------
    # 每块字符预算。中文约 1 字 ≈ 1 token，6000 字给"意见清单 + 修订文"的输出留足余量。
    # 整篇不超过这个值就不分块，走原来的单发路径（短文不该因为上图而变慢）
    longdoc_chunk_chars: int = 6000
    # 总开关。关掉后长文档仍单发，用于和分块路径做效果对比（评测台 A/B）
    longdoc_enabled: bool = True
    # LangGraph 检查点文件（相对 server/，也可写绝对路径）。
    # 同一 log_id 重跑时已完成的块不再重复计费；文件会一直长，属运维清理项
    checkpoint_path: str = "checkpoints/grading.sqlite3"

    # ---------- 日志 ----------
    # root logger 级别。排查重试/并发问题时调成 DEBUG
    log_level: str = "INFO"

    class Config:
        env_file = ENV_FILE  # 指定 .env 文件路径
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
