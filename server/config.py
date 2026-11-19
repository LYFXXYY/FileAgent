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

    # ---------- Postgres 连接 ----------
    # SQLAlchemy + psycopg2 的连接字符串格式：
    #   postgresql+psycopg2://user:password@host:port/database
    # 阶段 4 起库换成 Postgres（pgvector 镜像，宿主机端口 5434）
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
    # 读取阶段超时（秒）：等响应收完。长文档、批改类任务要放宽。
    # 实测参考：1117 字的中文 PDF 走批改，思考模型单次要 240s（关掉思考 12s），
    # 所以开着思考的端点必须把这里调到 300 左右，否则会重试到耗尽后失败
    llm_timeout_read: float = 180.0
    # 重试次数（0 = 不重试）。只对网络错误 / 超时 / 429 / 5xx 生效，其他 4xx 不重试
    llm_max_retries: int = 2
    # 进程内并发闸：同时在飞的 LLM 请求数。多 worker 部署下每个进程各算各的
    llm_max_concurrency: int = 4
    # 是否发送 enable_thinking 参数（DashScope 兼容端的思考开关）。
    # 默认 None = 请求体里不带这个字段，保持各家端点的原有行为；
    # 显式设 false 可把思考模型从 240s 压到 12s，代价是可能降低批改质量
    llm_enable_thinking: bool | None = None
    # 是否让 httpx 读系统代理环境变量。访问需要代理的境外端点时要 True；
    # 但 NO_PROXY 里若有非法写法（如裸 [::1]）会导致客户端构造直接报错，此时设 False
    llm_trust_env_proxy: bool = True

    # ---------- 长文档分块批改（阶段 3）----------
    # 每块字符预算。中文约 1 字 ≈ 1 token，6000 字给"意见清单 + 修订文"的输出留足余量。
    # 整篇不超过这个值就不分块，走原来的单发路径（短文不该因为上图而变慢）
    longdoc_chunk_chars: int = 6000
    # 总开关。关掉后长文档仍单发，用于和分块路径做效果对比（评测台 A/B）
    longdoc_enabled: bool = True
    # ⚠ 阶段 4 起分块批改的检查点和按块缓存都落 Postgres（与业务表同库），不再有
    # SQLite 文件配置项。原 `CHECKPOINT_PATH` 字段已撤；老 .env 里残留这行不影响。

    # ---------- 规范文档入库（阶段 4）----------
    # 规范库每块字符预算，与批改的 LONGDOC_CHUNK_CHARS 解耦：
    # - 批改链路：块越大 LLM 上下文越完整、改得越准；
    # - 检索链路（阶段 6）：规范库按**条款边界**切，一块就是一条完整条款，
    #   所以这个值不是装箱目标，而是"单条条款的上限"——超过它的超长条款才退回按句细分。
    #   2026-11-19 实测：当装箱用时（2000 字装 11 条）会把条款从【判定标准】前截断。
    spec_chunk_chars: int = 2000

    # ---------- 规范库向量检索（阶段 6）----------
    # embedding 端点刻意**不给默认值**：本机 LM Studio 的端口不固定（随上次启动漂移），
    # 写死一个"看起来能用"的默认值，换来的是连到别的模型上、维度对不上还静默入库。
    # 留空 = 未配置，任何编码调用都会响亮报错并提示去 .env 填。
    embedding_base_url: str = ""
    embedding_model: str = ""
    # 模型名必须写死并校验维度：同一服务上常挂着多种维度的 embedding 模型
    # （实测 bge-m3=1024、nomic-v1.5=768），选错是响亮失败而不是静默混存
    embedding_dimensions: int = 1024
    # 本地服务一般无需鉴权；云端端点填这里（不进 git）
    embedding_api_key: str = ""
    # 编码是短请求，不需要 LLM 那套 180s；但整批一次发，留 60s 余量
    embedding_timeout: float = 60.0
    # bge 系列做检索是**非对称**的：文档端不加前缀，查询端要加。
    # GGUF 元数据里没有任何前缀信息，必须由应用侧自己拼（实测 top1 0.4643 → 0.5143 且排序改变）
    embedding_query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    # 入库时同步编码。关掉则文本照常入库、向量留给回填脚本
    spec_embed_on_ingest: bool = True
    # 检索返回几条条款。规范条款平均 165 字，5 条 ≈ 800 字上下文
    spec_retrieval_top_k: int = 5
    # 注入批改 prompt 的条款上下文字数上限，超了从末尾截断（宁少不爆上下文）
    spec_context_max_chars: int = 3000

    # ---------- PPT 渲染（阶段 5 s5.4）----------
    # 'stub'（默认）= 1×1 占位 PNG，便携、快、单测/e2e 用；
    # 'soffice' = 经 WSL LibreOffice 真渲染（需 WSL2 + libreoffice-impress，见 pptx_renderer）
    ppt_render_backend: str = "stub"
    # 真渲染的出图 DPI。72 太糊、200 太大，100 是给 VLM 看图和留审计之间的折中
    ppt_render_dpi: int = 100
    # soffice 单次转换超时（秒）。冷启动 + 中文字体加载实测 3–8s/份，留足余量
    ppt_render_timeout: float = 90.0

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
