"""
api/v1 包 —— 汇总所有 v1 路由

【Java 对比】
- 类似 Spring 的 @EnableWebMvc 扫描 @Controller —— 这里显式组合
- main.py 只需 include_router(api_router, prefix="/api/v1")
"""

from fastapi import APIRouter

from api.v1 import auth, logs, process, providers, specs, tasks

# 顶层聚合路由，所有子路由都挂到它下面
api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(providers.router, prefix="/providers", tags=["模型提供商"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["任务"])
api_router.include_router(process.router, prefix="", tags=["文档处理"])
api_router.include_router(logs.router, prefix="/logs", tags=["操作日志"])
api_router.include_router(specs.router, prefix="/specs", tags=["规范库"])
