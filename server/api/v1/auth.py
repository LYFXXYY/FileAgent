"""
认证路由 —— 注册 / 登录

【Java 对比】
- APIRouter ≈ @RestController
- @router.post("/register") ≈ @PostMapping("/register")
- Depends(get_db) ≈ 自动注入的 JdbcTemplate / EntityManager
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import get_settings
from database import db_now, get_db
from middleware.auth import get_current_user
from models.user import User
from schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from services.auth import create_access_token, hash_password, verify_password
from services.defaults import init_user_defaults
from services.errors import CODE_LOGIN_LOCKED, CODE_QUOTA_REGISTER, tag

router = APIRouter()
settings = get_settings()

# 登录失败计数：{(客户端IP, 用户名小写): [失败时刻, ...]}
#
# 为什么是进程内字典而不是建表：这是一道"拖慢在线爆破"的闸，不是审计记录——
# 重启清零完全可接受，而建表要动 schema（§5 无 Alembic，改表成本远高于收益）。
# ⚠ 与 §3.3 的 reaper、§3.6 的并发闸同属**单 worker 假设**：多进程部署时每进程各算各的。
_FAILED_LOGINS: dict[tuple[str, str], list] = {}


def _failed_key(request: Request, username: str) -> tuple[str, str]:
    """
    按 (IP, 用户名) 计数，而不是只按用户名

    只按用户名的话，攻击者可以让受害者一直处在"被锁定"状态（拿错密码把别人锁在门外）；
    只按 IP 的话，分布式脚本就完全不受影响。两个键合起来才同时挡住这两种。
    注意 `request.client.host` 在反向代理后面会全是代理 IP——上 Nginx 之后必须改读
    X-Forwarded-For 并校验代理链，否则这道闸退化成"按 IP 全局锁定"。
    """
    ip = request.client.host if request.client else "unknown"
    return (ip, username.lower())


def _recent_failures(key: tuple[str, str], now, window: timedelta) -> list:
    times = [t for t in _FAILED_LOGINS.get(key, []) if now - t < window]
    if times:
        _FAILED_LOGINS[key] = times
    else:
        _FAILED_LOGINS.pop(key, None)
    # 顺带收敛内存：字典只增不减的话，被人拿随机用户名打一轮就留下了永久占位
    if len(_FAILED_LOGINS) > 10000:
        for k in [k for k, v in _FAILED_LOGINS.items() if not [t for t in v if now - t < window]]:
            _FAILED_LOGINS.pop(k, None)
    return times


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
    - 只需处理业务异常（用户名重复）与全局限额
    """
    # 24 小时全局注册上限。为什么必须有：MAX_PROCESSES_PER_DAY 是**按账号**算的，
    # 脚本注册 50 个号就有 50 份额度，兜住总消费的其实是这道闸。0 = 不限制。
    cap = settings.max_registrations_per_day
    if cap > 0:
        cutoff = db_now(db) - timedelta(hours=24)
        recent = db.query(func.count(User.id)).filter(User.created_at >= cutoff).scalar() or 0
        if recent >= cap:
            raise HTTPException(
                429, tag("今日新注册名额已用完，请明天再试或联系管理员", CODE_QUOTA_REGISTER)
            )

    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 注册路径**永远**只能建出 user：role 走模型默认值，不读 body。
    # 唯一的 admin 由 `api/v1/admin.py:update_role` 的护栏 0 固定住，不可再产生第二个。
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)  # 重新加载以拿到自增 id 和 server_default 的 created_at

    # 默认配置（4 任务 + 通用参考文档）：内部旁路，失败不影响注册结果
    init_user_defaults(db, user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    用户登录 —— 返回 JWT access_token

    安全考虑：不区分"用户不存在"和"密码错误"，统一返回 401，
    防止攻击者通过错误消息枚举用户名
    """
    window = timedelta(minutes=settings.login_lockout_minutes)
    limit = settings.login_max_failures
    key = _failed_key(request, body.username)
    now = db_now(db)

    if limit > 0:
        times = _recent_failures(key, now, window)
        if len(times) >= limit:
            # 不告诉攻击者还剩几次，也不告诉他这个用户名存不存在
            wait = max(1, int((window - (now - times[0])).total_seconds() // 60) + 1)
            raise HTTPException(
                429, tag(f"登录失败次数过多，请约 {wait} 分钟后再试", CODE_LOGIN_LOCKED)
            )

    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        if limit > 0:
            _FAILED_LOGINS.setdefault(key, []).append(now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    # 成功即清账：锁定只针对"连续失败"，不该让之前试错过的人背着历史记录
    _FAILED_LOGINS.pop(key, None)

    token = create_access_token(user.id, user.username)
    # role / can_use_default 随登录下发，供前端首屏展示；启动时 /auth/me 再刷一次
    # 快照（见 me()），管理员改授权后无需重登。
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role,
        can_use_default=user.can_use_default,
    )
