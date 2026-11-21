"""
认证路由 —— 注册 / 登录

【Java 对比】
- APIRouter ≈ @RestController
- @router.post("/register") ≈ @PostMapping("/register")
- Depends(get_db) ≈ 自动注入的 JdbcTemplate / EntityManager
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from middleware.auth import get_current_user
from models.user import User
from schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from services.auth import create_access_token, hash_password, verify_password
from services.defaults import init_user_defaults

router = APIRouter()


@router.get("/me", response_model=UserResponse)
def me(current: User = Depends(get_current_user)):
    """
    当前登录用户信息

    存在的唯一理由：role / can_use_default 是登录瞬间冻结进 localStorage 的快照，
    admin 改了某人之后那人要重登才生效——体验上是"我明明被授权了界面没变"。
    前端启动时调一次本接口刷新，管理员的变更下一屏就到位，不需要重登。
    """
    return current


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """
    用户注册

    - Pydantic 已经在进入函数体前完成参数校验（长度、正则）
    - 只需处理业务异常（用户名重复）
    """
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)  # 重新加载以拿到自增 id 和 server_default 的 created_at

    # 默认配置（4 任务 + 通用参考文档）：内部旁路，失败不影响注册结果
    init_user_defaults(db, user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """
    用户登录 —— 返回 JWT access_token

    安全考虑：不区分"用户不存在"和"密码错误"，统一返回 401，
    防止攻击者通过错误消息枚举用户名
    """
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_access_token(user.id, user.username)
    # role / can_use_default 随登录下发，供前端首屏展示；启动时 /auth/me 再刷一次
    # 快照（见 me()），管理员改授权后无需重登。
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role,
        can_use_default=user.can_use_default,
    )
