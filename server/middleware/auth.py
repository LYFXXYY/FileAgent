"""
JWT 认证依赖 —— 从请求头解析 token 并返回当前用户

【Java 对比】
- 类似 Spring Security 的 @AuthenticationPrincipal 参数解析器
- 也类比 HandlerMethodArgumentResolver —— 自动把 HTTP 请求中的信息转成方法参数

【用法】
  from fastapi import Depends
  from middleware.auth import get_current_user

  @router.get("/xxx")
  async def handler(user: User = Depends(get_current_user)):
      ...
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database import get_db
from models.user import User
from services.auth import decode_access_token

# HTTPBearer 自动从 "Authorization: Bearer xxx" 头提取 token
# 类比 Spring 的 BearerTokenResolver
# auto_error=False → 手动抛异常，方便自定义错误消息
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI 依赖：从 JWT 中解析出当前用户

    creds 由 HTTPBearer 自动填充，格式 = Bearer <token>
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或 token 无效",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if creds is None or not creds.credentials:
        raise unauthorized

    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise unauthorized

    # payload["sub"] 是字符串形式的 user_id（JWT 标准要求 sub 为 string）
    user_id_str = payload.get("sub")
    if not user_id_str or not user_id_str.isdigit():
        raise unauthorized

    user = db.query(User).filter(User.id == int(user_id_str)).first()
    if user is None:
        # token 有效但用户已被删除
        raise unauthorized
    return user


def require_admin(current: User = Depends(get_current_user)) -> User:
    """
    管理端点专用依赖：非 admin 一律 403

    与 §3.1 的关系：数据隔离约束管的是普通用户路由；admin 路由**有意**跨用户查数据，
    新加管理功能时不要再"顺手"给查询补 user_id 过滤，否则 admin 视图直接失效。
    """
    if current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current
