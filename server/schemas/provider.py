"""模型提供商相关的请求/响应模型"""

from datetime import datetime

from pydantic import BaseModel, Field


class ProviderCreate(BaseModel):
    """新增提供商"""
    name: str = Field(min_length=1, max_length=50)
    base_url: str = Field(max_length=255, description="API 基础地址，如 https://ark.cn-beijing.volces.com/api/v3")
    api_key: str = Field(max_length=255)
    model_name: str = Field(max_length=100)
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.7, ge=0, le=2)


class ProviderUpdate(BaseModel):
    """更新提供商 —— 所有字段可选，只改传了的字段"""
    # 用 Optional 让前端可以只填要改的字段（PATCH 语义）
    name: str | None = Field(default=None, max_length=50)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=255)
    model_name: str | None = Field(default=None, max_length=100)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    is_active: bool | None = None


class ProviderResponse(BaseModel):
    """对外响应（屏蔽 api_key 敏感字段——只返回掩码后的）"""
    id: int
    name: str
    base_url: str
    # 不返回真实 api_key，返回前 4 位 + ****
    api_key_masked: str
    model_name: str
    max_tokens: int
    temperature: float
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_masked(cls, obj) -> "ProviderResponse":
        """从 ORM 对象构造响应，把 api_key 打码"""
        return cls(
            id=obj.id,
            name=obj.name,
            base_url=obj.base_url,
            api_key_masked=(obj.api_key[:4] + "****") if len(obj.api_key) > 4 else "****",
            model_name=obj.model_name,
            max_tokens=obj.max_tokens,
            temperature=obj.temperature,
            is_active=obj.is_active,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )
