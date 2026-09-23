"""
模型提供商 CRUD 路由

【权限模型】
- 每个提供商归属创建它的用户，所有操作按 user_id 隔离（和 Task 一致）
- 用户只能看到 / 修改 / 删除自己的提供商，杜绝越权读写他人 API Key

【同步 vs 异步】
- 这里 CRUD 只用数据库，用 def 就够（FastAPI 会自动扔到线程池跑）
- 涉及外部 HTTP 调用（如 LLM）的路由才必须 async def
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from middleware.auth import get_current_user
from models.provider import ModelProvider
from models.task import Task
from models.user import User
from schemas.provider import ProviderCreate, ProviderResponse, ProviderUpdate

router = APIRouter()


@router.get("", response_model=list[ProviderResponse])
def list_providers(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """列出当前用户的提供商（含未启用的，前端自行按 is_active 过滤展示）"""
    providers = (
        db.query(ModelProvider)
        .filter(ModelProvider.user_id == current.id)
        .order_by(ModelProvider.id)
        .all()
    )
    return [ProviderResponse.from_orm_masked(p) for p in providers]


@router.post("", response_model=ProviderResponse, status_code=201)
def create_provider(
    body: ProviderCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    # user_id 从登录态取，绝不接受请求体传入（防越权写他人名下）
    provider = ModelProvider(user_id=current.id, **body.model_dump())
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return ProviderResponse.from_orm_masked(provider)


@router.put("/{provider_id}", response_model=ProviderResponse)
def update_provider(
    provider_id: int,
    body: ProviderUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    provider = (
        db.query(ModelProvider)
        .filter(ModelProvider.id == provider_id, ModelProvider.user_id == current.id)
        .first()
    )
    if provider is None:
        # 404 而非 403：不暴露"提供商存在但不属于你"
        raise HTTPException(404, "提供商不存在")

    # exclude_unset=True → 只更新前端实际传了的字段，未传的保持原值
    # 类比 Java 的 BeanUtils.copyProperties(src, target, ignoreProps)
    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(provider, key, value)

    db.commit()
    db.refresh(provider)
    return ProviderResponse.from_orm_masked(provider)


@router.delete("/{provider_id}", status_code=204)
def delete_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    provider = (
        db.query(ModelProvider)
        .filter(ModelProvider.id == provider_id, ModelProvider.user_id == current.id)
        .first()
    )
    if provider is None:
        raise HTTPException(404, "提供商不存在")

    # 先把引用该提供商的任务 provider_id 置空，否则 MySQL 外键约束会让删除失败（500）
    # 任务转为"未指定提供商"，处理时由系统选当前用户的默认 active 提供商
    db.query(Task).filter(Task.provider_id == provider_id).update(
        {Task.provider_id: None}, synchronize_session=False
    )
    db.delete(provider)
    db.commit()
    return None
