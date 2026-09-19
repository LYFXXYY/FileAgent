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
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.task import TaskCreate, TaskResponse, TaskUpdate

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
    # 如果指定了 provider_id，验证其存在
    if body.provider_id is not None:
        if db.query(ModelProvider).get(body.provider_id) is None:
            raise HTTPException(400, "指定的模型提供商不存在")

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

    for key, value in body.model_dump(exclude_unset=True).items():
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
    db.delete(task)
    db.commit()
    return None
