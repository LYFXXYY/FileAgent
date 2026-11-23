"""
FileAgent 后端入口 —— FastAPI 应用 + 中间件 + 生命周期管理

【Java 对比】
- @app.on_event("startup")  ≈  @PostConstruct（应用启动后执行）
- app.add_middleware(...)    ≈  实现 Filter / HandlerInterceptor 注册到 WebMvcConfigurer
- 路由 @app.get("/path")    ≈  @GetMapping + @RequestMapping
"""

import asyncio
import logging  # 结构化日志（替代 print，避免 Windows GBK 控制台中文乱码）
import sys  # 日志输出到 stdout
from contextlib import asynccontextmanager  # 管理 startup/shutdown 的上下文管理器

# Windows + psycopg3 异步模式硬冲突：默认 ProactorEventLoop 不支持 socket 异步读写，
# AsyncPostgresSaver / AsyncPostgresStore 一调用就炸 "Psycopg cannot use the
# 'ProactorEventLoop' to run in async mode"。必须在任何异步对象创建之前切到
# SelectorEventLoop。httpx / FastAPI / LangGraph 全兼容，性能差异可忽略。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func

# 导入 database 模块和所有模型 —— 让 Base.metadata 收集到建表信息
import database
import models  # noqa: F401    （导入触发 models/__init__.py 里的所有 ORM 类注册）
from api.v1 import api_router  # 汇总好的 v1 路由
from config import get_settings, normalize_no_proxy_env
from services.llm import LLMError, close_client, get_client
from services.logging_setup import attach_uvicorn_logs, build_file_handler

logger = logging.getLogger("fileagent")

LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"


def _setup_logging() -> logging.Handler | None:
    """
    配置 root logger —— 项目此前一个 handler 都没装，root 停在 WARNING，
    结果 logger.info(...) 全部静默丢弃，只有 WARNING 及以上经 lastResort 打到 stderr。
    重试、并发、计量这类运行信息看不见，调优就无从谈起，所以这里补上。

    force=True：--reload 会反复重新导入本模块，不清掉旧 handler 每行会打印两遍。

    返回文件 handler（没配 LOG_FILE 时是 None），交给 lifespan 再挂给 uvicorn ——
    见 `services/logging_setup` 的模块 docstring：uvicorn 启动时会重配自己的 logger，
    这一步在 import 期做不掉。
    """
    settings = get_settings()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    handlers: list[logging.Handler] = [stream]

    file_handler = build_file_handler(
        settings.log_file, settings.log_max_mb * 1024 * 1024, settings.log_backup_count
    )
    if file_handler:
        handlers.append(file_handler)

    logging.basicConfig(level=settings.log_level.upper(), handlers=handlers, force=True)
    return file_handler


_FILE_HANDLER = _setup_logging()

# trust_env=True 的 httpx 客户端构造前必须清掉裸 [::1]（见 config 函数注释）。
# mineru._get_client 也会调一次（独立脚本不经过 main），幂等无副作用
for _key in normalize_no_proxy_env():
    logger.info("已从 %s 移除裸 [::1]（httpx 无法解析）", _key)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期 —— startup 和 shutdown 阶段的钩子

    yield 前 = 应用启动时执行（类似 @PostConstruct）
    yield 后 = 应用关闭时执行（类似 @PreDestroy，如关连接池）

    【为什么用 lifespan 而不是 on_event？】
    on_event("startup") 在 FastAPI 0.93+ 已标记 deprecated，
    lifespan 是推荐写法，可以访问 app.state、更清晰地管理资源。
    """
    # ---------- Startup ----------
    # uvicorn 已经按它自己的 dictConfig 配好 logger 了，文件 handler 只能现在补挂，
    # 否则访问日志（谁在打这个服务）永远只进 stdout
    attach_uvicorn_logs(_FILE_HANDLER)
    # create_all(): 检查数据库中是否存在这些表，不存在则 CREATE TABLE
    # 类比 Hibernate 的 spring.jpa.hibernate.ddl-auto=update
    # ⚠ 仅适合开发阶段！生产环境用 Alembic 做 schema 迁移
    database.Base.metadata.create_all(bind=database.engine)
    logger.info("数据库连接成功，表已创建/验证")
    # 启动期把 event_loop 类型打出来：psycopg3 异步模式只认 Selector，Proactor 直接卡死。
    # 看到 Proactor 就意味着用了 `uvicorn main:app` 而非 run_backend.py——长文档分块会炸。
    logger.info("event_loop=%s", type(asyncio.get_running_loop()).__name__)

    # 僵尸状态清理：上次进程崩溃/重启时，正在后台处理的日志会永远卡在 processing
    # （BackgroundTasks 不持久化，进程一停就丢）。启动时把它们重置为 failed，
    # 让用户能在日志页看到并重试，而不是前端无限轮询。
    # ⚠ 仅适用单 worker；多 worker 部署应改用带超时阈值的 reaper 或任务队列
    from models.operation_log import LogStatus, OperationLog

    with database.SessionLocal() as db:
        stale = db.query(OperationLog).filter(OperationLog.status == LogStatus.PROCESSING).all()
        for log in stale:
            log.status = LogStatus.FAILED
            log.error_message = "服务重启导致处理中断，请重试"
            # 模型约定"每个状态变更都记录 processed_at"，这里漏了会让已失败的日志没有完成时刻
            log.processed_at = func.now()
        if stale:
            db.commit()
            logger.info("已将 %d 条卡死的 processing 日志重置为 failed", len(stale))

    # 预热共享 LLM 客户端：构造期就会读的代理配置若有问题，让它在这里炸进启动日志，
    # 而不是等用户点"处理"时才第一次失败
    try:
        get_client()
        logger.info(
            "LLM 客户端就绪：并发上限 %s，读取超时 %ss，重试 %s 次",
            get_settings().llm_max_concurrency,
            get_settings().llm_timeout_read,
            get_settings().llm_max_retries,
        )
    except LLMError:
        # 不阻断启动：其余接口照常可用，LLM 调用各自报错并写进 error_message
        logger.exception("LLM 客户端初始化失败，检查 LLM_TRUST_ENV_PROXY 与系统代理环境变量")

    yield  # ← 应用在这里运行，上面的代码是 startup，下面的代码是 shutdown

    # ---------- Shutdown ----------
    logger.info("释放 LLM 与数据库连接池")
    await close_client()
    # engine.dispose() 关闭连接池（uvicorn 退出时通常自动清理）
    database.engine.dispose()


# ---------- FastAPI 实例 ----------
# lifespan 参数指定启动/关闭函数
# description 会显示在 Swagger 首页，顺手把项目定位讲清：已不止 Word
app = FastAPI(
    title="FileAgent",
    description="文档 AI 处理平台：上传文档 → 按自定义 Prompt 调 LLM → 下载结果；"
    "批改类任务额外产出可定位的问题清单",
    lifespan=lifespan,
)

# ---------- CORS 中间件 ----------
# 让前端能跨域访问后端 API
# 类比 Spring 的 @CrossOrigin 或全局 CorsConfiguration
# 【配置方式】通过 .env 的 CORS_ORIGINS 指定，逗号分隔多个源
#   例: CORS_ORIGINS=http://localhost:5173,https://example.com
#   未设置时默认只允许 localhost:5173（开发环境）
# 注意走 Settings 而非 os.getenv：pydantic-settings 不会把 .env 写进 os.environ，
# 用 os.getenv 读不到 .env 里的配置
_cors_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,  # 允许携带 Cookie / Authorization 头
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 健康检查 ----------
# 验证：后端服务是否存活 + 数据库是否连接
# 注意 async def —— FastAPI 推荐所有 handler 用 async，即使当前没有 await
@app.get("/api/health")
async def health():
    """健康检查 —— 返回服务状态和数据库连通性"""
    from sqlalchemy import text  # 局部导入：只在需要时引入，减少启动时间

    # 执行一条简单 SQL 验证数据库是否可达
    # 类比 JdbcTemplate.queryForObject("SELECT 1", Integer.class)
    with database.engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # 如果数据库挂了这里会抛异常
    return {"status": "ok", "database": "connected"}


# ---------- 挂载 v1 API 路由 ----------
# prefix="/api/v1" 让下面所有路由都自动带这个前缀
# 类比 Spring 的 server.servlet.context-path
app.include_router(api_router, prefix="/api/v1")
