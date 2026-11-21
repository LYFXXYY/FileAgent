"""
任务 CRUD 路由

【数据隔离】
- 每个用户只能看到 / 修改自己的任务（WHERE user_id = current_user.id）
- 类比 Spring Security 的 @PreAuthorize("hasRole('OWNER')")
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from middleware.auth import get_current_user
from models.operation_log import OperationLog
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.task import TaskCreate, TaskResponse, TaskUpdate
from services.llm import provider_usable_by

router = APIRouter()


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """只返回当前用户的任务"""
    return db.query(Task).filter(Task.user_id == current.id).order_by(Task.id).all()


@router.post("", response_model=TaskResponse, status_code=201)
def create_task(
    body: TaskCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    # 如果指定了 provider_id，验证存在且对该用户可用（自己的或被授权的公共提供商）
    if body.provider_id is not None:
        provider = db.get(ModelProvider, body.provider_id)
        if provider is None or not provider_usable_by(provider, current):
            raise HTTPException(400, "指定的模型提供商不存在或不可用")

    task = Task(user_id=current.id, **body.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.put("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    body: TaskUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    task = db.query(Task).filter(Task.id == task_id, Task.user_id == current.id).first()
    if task is None:
        # 404 而不是 403：不暴露"任务存在但不属于你"这一信息
        raise HTTPException(404, "任务不存在")

    updates = body.model_dump(exclude_unset=True)
    # 若改动 provider_id，同样校验可用性（含公共提供商授权）
    new_provider_id = updates.get("provider_id")
    if new_provider_id is not None:
        provider = db.get(ModelProvider, new_provider_id)
        if provider is None or not provider_usable_by(provider, current):
            raise HTTPException(400, "指定的模型提供商不存在或不可用")

    # md/pptx + 批改的互斥要在合并后的值上判：单改其中一个字段也可能凑出非法组合
    merged_format = updates.get("output_format", task.output_format)
    merged_grading = updates.get("is_grading", task.is_grading)
    if merged_format in ("md", "pptx") and merged_grading:
        raise HTTPException(400, "md / pptx 转换任务不能同时是批改类任务")

    for key, value in updates.items():
        setattr(task, key, value)
    db.commit()
    db.refresh(task)
    return task


@router.delete("/{task_id}", status_code=204)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    task = db.query(Task).filter(Task.id == task_id, Task.user_id == current.id).first()
    if task is None:
        raise HTTPException(404, "任务不存在")

    # 先把引用该任务的历史日志 task_id 置空，否则外键约束会让删除失败（500）
    # 日志已冗余存 task_name，置空后仍可读
    db.query(OperationLog).filter(OperationLog.task_id == task_id).update(
        {OperationLog.task_id: None}, synchronize_session=False
    )
    db.delete(task)
    db.commit()
    return None
