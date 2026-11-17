"""
管理路由（SPEC §9.11）—— 用户列表 / 角色升降 / 默认模型授权开关

【为什么 admin 路由可以不守 user_id 过滤】
§3.1 的隔离铁律管的是普通用户路由；管理功能**定义上**就要跨用户读写。
两条入口都挂了 require_admin，非 admin 一律 403。新加管理端点时照抄这个依赖。

【两道防自锁护栏】
1. 不能改**自己**的角色——把唯一的自己降成 user 就再也没人能操作管理页了；
2. 不能把**最后一个 admin** 降权——即使通过别的账号操作，也要保证系统里永远有管理员。
前端会把对应按钮 disable，后端是最终防线。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from middleware.auth import require_admin
from models.llm_call import LlmCall
from models.operation_log import OperationLog
from models.task import Task
from models.user import User
from schemas.admin import AdminUserRow, DefaultAccessUpdate, RoleUpdate

router = APIRouter()


def _row(db: Session, user: User) -> AdminUserRow:
    """单个用户 + 即时聚合用量。N 个用户 N 次聚合查询——管理页低频且量小，够用"""
    task_count = db.query(func.count(Task.id)).filter(Task.user_id == user.id).scalar() or 0
    log_count = (
        db.query(func.count(OperationLog.id)).filter(OperationLog.user_id == user.id).scalar() or 0
    )
    total_tokens = (
        db.query(func.coalesce(func.sum(LlmCall.total_tokens), 0))
        .filter(LlmCall.user_id == user.id)
        .scalar()
        or 0
    )
    return AdminUserRow(
        id=user.id,
        username=user.username,
        role=user.role,
        can_use_default=user.can_use_default,
        created_at=user.created_at,
        task_count=task_count,
        log_count=log_count,
        total_tokens=int(total_tokens),
    )


@router.get("/users", response_model=list[AdminUserRow])
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """全部用户（跨用户读是管理功能的定义，不是隔离漏洞）"""
    users = db.query(User).order_by(User.id).all()
    return [_row(db, u) for u in users]


@router.patch("/users/{user_id}/role", response_model=AdminUserRow)
def update_role(
    user_id: int,
    body: RoleUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    # 护栏 1：禁止自助改角色（防自锁的唯一入口）
    if user_id == admin.id:
        raise HTTPException(400, "不能修改自己的角色")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    # 护栏 2：系统里至少留一个 admin
    if target.role == "admin" and body.role != "admin":
        remaining = (
            db.query(func.count(User.id))
            .filter(User.role == "admin", User.id != target.id)
            .scalar()
            or 0
        )
        if remaining == 0:
            raise HTTPException(400, "系统必须保留至少一名管理员")
    target.role = body.role
    db.commit()
    db.refresh(target)
    return _row(db, target)


@router.patch("/users/{user_id}/default-access", response_model=AdminUserRow)
def update_default_access(
    user_id: int,
    body: DefaultAccessUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    开/关某用户使用默认（公共）模型的授权

    吊销语义（§9.11.3-3）：**正在后台跑的任务不打断**——已经发生的外呼要记账、
    用户应拿到结果；下一次发起处理时 resolve_provider 会拒绝并报"提供商不可用"。
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    target.can_use_default = body.can_use_default
    db.commit()
    db.refresh(target)
    return _row(db, target)
