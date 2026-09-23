"""
FileAgent 后端入口 —— FastAPI 应用 + 中间件 + 生命周期管理

【Java 对比】
- @app.on_event("startup")  ≈  @PostConstruct（应用启动后执行）
- app.add_middleware(...)    ≈  实现 Filter / HandlerInterceptor 注册到 WebMvcConfigurer
- 路由 @app.get("/path")    ≈  @GetMapping + @RequestMapping
"""

import logging  # 结构化日志（替代 print，避免 Windows GBK 控制台中文乱码）
import os  # 读取环境变量（CORS 配置）
from contextlib import asynccontextmanager  # 管理 startup/shutdown 的上下文管理器

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 导入 database 模块和所有模型 —— 让 Base.metadata 收集到建表信息
import database
import models  # noqa: F401    （导入触发 models/__init__.py 里的所有 ORM 类注册）
from api.v1 import api_router  # 汇总好的 v1 路由

logger = logging.getLogger("fileagent")


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
    # create_all(): 检查数据库中是否存在这些表，不存在则 CREATE TABLE
    # 类比 Hibernate 的 spring.jpa.hibernate.ddl-auto=update
    # ⚠ 仅适合开发阶段！生产环境用 Alembic 做 schema 迁移
    database.Base.metadata.create_all(bind=database.engine)
    logger.info("MySQL 连接成功，表已创建/验证")

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
        if stale:
            db.commit()
            logger.info("已将 %d 条卡死的 processing 日志重置为 failed", len(stale))

    yield  # ← 应用在这里运行，上面的代码是 startup，下面的代码是 shutdown

    # ---------- Shutdown ----------
    # 可选：engine.dispose() 关闭连接池（uvicorn 退出时通常自动清理）
    logger.info("释放数据库连接池")
    database.engine.dispose()


# ---------- FastAPI 实例 ----------
# lifespan 参数指定启动/关闭函数
app = FastAPI(title="FileAgent", lifespan=lifespan)

# ---------- CORS 中间件 ----------
# 让前端能跨域访问后端 API
# 类比 Spring 的 @CrossOrigin 或全局 CorsConfiguration
# 【配置方式】通过环境变量 CORS_ORIGINS 指定，逗号分隔多个源
#   例: CORS_ORIGINS=http://localhost:5173,https://example.com
#   未设置时默认只允许 localhost:5173（开发环境）
_cors_raw = os.getenv("CORS_ORIGINS", "http://localhost:5173")
_cors_origins = [origin.strip() for origin in _cors_raw.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,  # 允许携带 Cookie / Authorization 头
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 健康检查 ----------
# 验证：后端服务是否存活 + MySQL 是否连接
# 注意 async def —— FastAPI 推荐所有 handler 用 async，即使当前没有 await
@app.get("/api/health")
async def health():
    """健康检查 —— 返回服务状态和数据库连通性"""
    from sqlalchemy import text  # 局部导入：只在需要时引入，减少启动时间

    # 执行一条简单 SQL 验证 MySQL 是否可达
    # 类比 JdbcTemplate.queryForObject("SELECT 1", Integer.class)
    with database.engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # 如果 MySQL 挂了这里会抛异常
    return {"status": "ok", "database": "connected"}


# ---------- 挂载 v1 API 路由 ----------
# prefix="/api/v1" 让下面所有路由都自动带这个前缀
# 类比 Spring 的 server.servlet.context-path
app.include_router(api_router, prefix="/api/v1")
