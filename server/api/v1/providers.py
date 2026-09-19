"""
模型提供商 CRUD 路由

【权限模型】
- 所有用户权限相同，任何人可以增删改任何提供商（按项目设计决定）
- 若之后要区分管理员，加个 requires_admin 依赖即可

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
from models.user import User
from schemas.provider import ProviderCreate, ProviderResponse, ProviderUpdate

router = APIRouter()


@router.get("", response_model=list[ProviderResponse])
def list_providers(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """列出所有提供商（含未启用的，前端自行按 is_active 过滤展示）"""
    providers = db.query(ModelProvider).order_by(ModelProvider.id).all()
    return [ProviderResponse.from_orm_masked(p) for p in providers]


@router.post("", response_model=ProviderResponse, status_code=201)
def create_provider(
    body: ProviderCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    provider = ModelProvider(**body.model_dump())  # 一次性把 DTO 转成 ORM
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return ProviderResponse.from_orm_masked(provider)


@router.put("/{provider_id}", response_model=ProviderResponse)
def update_provider(
    provider_id: int,
    body: ProviderUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    provider = db.query(ModelProvider).filter(ModelProvider.id == provider_id).first()
    if provider is None:
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
    _: User = Depends(get_current_user),
):
    provider = db.query(ModelProvider).filter(ModelProvider.id == provider_id).first()
    if provider is None:
        raise HTTPException(404, "提供商不存在")
    db.delete(provider)
    db.commit()
    return None
