"""
操作日志查询路由 —— 分页 + 只看自己的

【分页设计】
- page 从 1 开始，page_size 默认 20，最大 100
- 类比 Java PageRequest.of(page - 1, size)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from middleware.auth import get_current_user
from models.operation_log import OperationLog
from models.user import User
from schemas.operation_log import LogListResponse, LogResponse

router = APIRouter()


@router.get("", response_model=LogListResponse)
def list_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """分页查询当前用户的操作日志（时间倒序）"""
    q = db.query(OperationLog).filter(OperationLog.user_id == current.id)
    total = q.count()
    items = (
        q.order_by(OperationLog.uploaded_at.desc(), OperationLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return LogListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[LogResponse.model_validate(i) for i in items],
    )


@router.get("/{log_id}", response_model=LogResponse)
def get_log(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """查看单条日志（详情）"""
    log = db.query(OperationLog).filter(
        OperationLog.id == log_id, OperationLog.user_id == current.id
    ).first()
    if log is None:
        from fastapi import HTTPException
        raise HTTPException(404, "记录不存在")
    return log
