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


def normalize_no_proxy_env() -> list[str]:
    """
    摘掉 NO_PROXY 里的裸 [::1]：httpx 解析它时抛 InvalidURL: Invalid port，
    所有 trust_env=True 的客户端（MinerU / 走系统代理的 LLM）构造即炸。
    本机 NO_PROXY 同时含 ::1 与 [::1]，去掉带括号的那份不改变豁免语义。
    只改进程内环境变量，不动系统设置。返回被改过的键名（供启动日志用）。
    """
    changed = []
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        entries = [e.strip() for e in value.split(",")]
        cleaned = [e for e in entries if e and e != "[::1]"]
        if len(cleaned) != len([e for e in entries if e]):
            os.environ[key] = ",".join(cleaned)
            changed.append(key)
    return changed


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

    # 产物留存天数（`cleanup_storage.py` 用）：磁盘文件与分块图状态共用。0 = 不做过期清理。
    # 只影响"下载/重试要用的东西"：批改与普通 docx 是下载时从库里现算的，
    # 不占磁盘，因此与这个数无关。
    retention_days: int = 30

    # ---------- 上传规模上限 ----------
    # 单个文件的字节上限（MB）。0 = 不限制。
    # 为什么必须有：上传是"先整份读进内存再提取"，没有上限时陌生人一个请求就能把
    # worker 内存吃光；而 PDF 还会整份传给 MinerU 云、**按页计费**，所以这条同时是
    # 内存闸和账单闸。默认 20 MB——本机最大的真实夹具是 4.3 MB 的护理指南 PDF。
    max_upload_mb: int = 20
    # PDF 页数上限。0 = 不限制。按页计费的东西必须单独限：20 MB 里塞得下一份
    # 两千页的文件，字节没超但钱会超。
    # 默认从 300 降到 50（2026-11-23）：局域网开放后算过最坏路径——
    # 转换类任务**不需要管理员授权模型**就能走 MinerU（`resolve_provider` 管的是 LLM，
    # MinerU 只看服务端 token），于是 300 页 × 每人 20 次/天 × 20 个新号/天 = 12 万页/天
    # 的账单敞口。50 页挡住绝大多数正常文档（本机最大真实夹具 26 页），
    # 真需要长文档的人由管理员临时上调。
    max_pdf_pages: int = 50

    # ---------- 密钥加密 ----------
    # 给 provider.api_key 做字段级加密的 Fernet 密钥（urlsafe base64 的 32 字节）。
    # 生成：python -c "from services.secrets import generate_key; print(generate_key())"
    # ⚠ 丢失即不可恢复：库里所有模型密钥都会解不开，只能让用户重新填。必须单独备份。
    # 留空 = 功能未启用，此时**保存或读取密钥都会响亮报错**，不会退回明文存储。
    api_key_enc_key: str = ""

    # ---------- 跨域 ----------
    # 允许的源，逗号分隔多个
    # 必须在 Settings 里声明：pydantic-settings 只把 .env 的值喂给本对象，
    # 不会写入 os.environ。用 os.getenv("CORS_ORIGINS") 读 .env 永远读到 None
    #
    # 默认值写 5241 而不是 5173：5173 落在本机 Windows 保留端口段（5141–5240）里
    # 永远绑不上，把它当默认值等于给新环境一个用不了的地址。
    # 另外正常路径其实用不到 CORS——前端走 Vite/Caddy 同源代理 `/api`，
    # 只有"前后端分开部署在不同源"时才需要动这里。
    cors_origins: str = "http://localhost:5241"

    # ---------- 提供商地址的 SSRF 防护 ----------
    # 用户自建 provider 的 base_url 是**由服务端发出请求**的，所以默认禁止指向
    # 环回 / 私有网段 / 链路本地（云上的 169.254.169.254 元数据服务就在那里）。
    # 置 true 会放开这条限制——**只应在开发机上为了连本地假端点或 LM Studio 时开**，
    # 部署给别人用时绝对不要带这一行。
    allow_private_provider_urls: bool = False

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
    # 每个用户 24 小时内最多触发多少次处理。**0 = 不限制**（默认值，保持单机开发与
    # 既有测试的行为逐字不变）。
    # 为什么需要它：每一次处理都在花真钱——LLM 按 token、MinerU 按页计费，而且失败重试
    # 会把整条链路再跑一遍。项目开放给别人用时，这是唯一能挡住"一个人脚本化刷爆账单"的闸。
    # 判据用 processed_at（处理完成时刻）而不是 uploaded_at：上传不等于消耗，
    # 只统计真正发生过处理的次数才对得上成本。
    max_processes_per_day: int = 0

    # ---------- 认证暴力破解防护 ----------
    # 同一个 (客户端 IP, 用户名) 在锁定窗口内最多允许多少次失败登录。0 = 不启用。
    # 计数器是**进程内**的：多 worker 下每个进程各算各的（同 §3.3 的 reaper、
    # §3.6 的并发闸同一个单 worker 假设），重启即清零。开放注册给别人用之前必须有，
    # 否则 /auth/login 就是一个可以无限试的免费口令校验接口。
    login_max_failures: int = 8
    # 锁定窗口（分钟），也是失败次数的遗忘时长
    login_lockout_minutes: int = 15
    # 24 小时内最多允许注册多少个新账号。0 = 不限制（默认，单机自用）。
    # 为什么光有 MAX_PROCESSES_PER_DAY 不够：那个配额按**账号**算，脚本注册 50 个号
    # 就有 50 份额度。注册上限才是真正兜住总消费的那道闸。
    max_registrations_per_day: int = 0
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
    # bge 系列做检索是否要加查询前缀，**取决于具体模型**：
    # `bge-large-zh-v1.5` 那一批是带指令训练的，官方要求查询端加
    # "为这个句子生成表示以用于检索相关文章："；而 **bge-m3 不是**——它训练时不带指令。
    # 本机实测（9 条夹具、66 条款、k=5）：加前缀 recall@5=0.380 / hit=0.778，
    # 不加 recall@5=0.491 / hit=0.889。所以默认留空，换模型时再按该模型的训练方式配。
    # 前缀不在 GGUF 元数据里，只能应用侧拼；加了也不会报错，只会让相似度整体漂移。
    embedding_query_prefix: str = ""
    # 入库时同步编码。关掉则文本照常入库、向量留给回填脚本
    spec_embed_on_ingest: bool = True
    # 批改前是否自动召回规范条款。关掉后批改链路与"阶段 6 之前"逐字一致，
    # 评测台靠它做 RAG A/B（同 longdoc_enabled 的思路）
    spec_rag_enabled: bool = True
    # 检索返回几条条款。规范条款平均 165 字，5 条 ≈ 800 字上下文
    spec_retrieval_top_k: int = 5
    # 召回方式：hybrid（默认，向量+词法 RRF 融合）/ vector / lexical。
    # 评测台靠这三个取值做消融，别把它当装饰性开关
    spec_retrieval_mode: str = "hybrid"
    # 注入批改 prompt 的条款上下文字数上限，超了从末尾截断（宁少不爆上下文）
    spec_context_max_chars: int = 3000
    # 查询滑窗：bge 对超长输入静默截断，整篇一次编码等于只拿开头去检索
    spec_query_window_chars: int = 800
    # 最多几个窗口。超出时等步长抽样（覆盖首尾比只取开头诚实）
    spec_query_max_windows: int = 4

    # ---------- PPT 渲染（阶段 5 s5.4）----------
    # 'stub'（默认）= 1×1 占位 PNG，便携、快、单测/e2e 用；
    # 'soffice' = 经 WSL LibreOffice 真渲染（需 WSL2 + libreoffice-impress，见 pptx_renderer）
    ppt_render_backend: str = "stub"
    # 真渲染的出图 DPI。72 太糊、200 太大，100 是给 VLM 看图和留审计之间的折中
    ppt_render_dpi: int = 100
    # soffice 单次转换超时（秒）。冷启动 + 中文字体加载实测 3–8s/份，留足余量
    ppt_render_timeout: float = 90.0
    # ---------- PPT 版面检查循环（② 片3，services/ppt_layout.py）----------
    # 关掉后即使配了 vision 提供商也不跑版面循环（只剩片2 坏块修复）
    ppt_layout_enabled: bool = True
    # 批评 VLM 调用次数上限（语义同 §3.11：pass 早停时实际更少；硬上限 5 在代码里）
    ppt_layout_max_rounds: int = 2

    # ---------- MinerU 扫描件兜底 ----------
    # 官方云 API token（mineru.net/apiManage 申请，有效期 90 天）。
    # 留空 = 功能关闭：扫描件照旧 400 拒收，行为与接入前逐字一致
    mineru_token: str = ""
    mineru_base_url: str = "https://mineru.net/api/v4"
    # 轮询间隔与最长等待（秒）。云 API 是异步任务制，排队 + 解析可达分钟级
    mineru_poll_interval: float = 3.0
    mineru_max_wait: float = 600.0
    # 单次 HTTP 请求超时（秒）：上传/下载 zip 用，与轮询总时长是两回事
    mineru_http_timeout: float = 60.0

    # ---------- 日志 ----------
    # root logger 级别。排查重试/并发问题时调成 DEBUG
    log_level: str = "INFO"
    # 运行日志落盘路径（相对 server/）。留空 = 只打 stdout、不落盘。
    # 落盘才谈得上"事后查问题"：stdout 重定向的文件每次重启就被覆盖
    log_file: str = "logs/backend.log"
    # 单个日志文件上限与保留份数（RotatingFileHandler），5 MB × 5 ≈ 最坏情况 25 MB
    log_max_mb: int = 5
    log_backup_count: int = 5

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
