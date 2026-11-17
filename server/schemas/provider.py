"""模型提供商相关的请求/响应模型"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# 能力标记的合法取值。加新能力（如 audio）时改这里和前端下拉框即可
KNOWN_CAPABILITIES = ("text", "vision", "embedding")


def _normalize_capabilities(value: str) -> str:
    """
    校验并规范化能力标记：小写、去重、按 KNOWN_CAPABILITIES 的固定顺序输出

    固定顺序是为了同一个集合永远序列化成同一个串，比对和展示都不会飘。
    非法值直接在入口拒掉，不让 "textv" 这种拼错的东西进库变成隐形能力缺失。
    """
    items = {c.strip().lower() for c in value.split(",") if c.strip()}
    if not items:
        raise ValueError(f"至少需要一个能力标记，可选 {', '.join(KNOWN_CAPABILITIES)}")

    unknown = items - set(KNOWN_CAPABILITIES)
    if unknown:
        raise ValueError(
            f"未知能力标记 {', '.join(sorted(unknown))}，可选 {', '.join(KNOWN_CAPABILITIES)}"
        )

    return ",".join(c for c in KNOWN_CAPABILITIES if c in items)


class ProviderCreate(BaseModel):
    """新增提供商"""

    name: str = Field(min_length=1, max_length=50)
    base_url: str = Field(
        max_length=255, description="API 基础地址，如 https://ark.cn-beijing.volces.com/api/v3"
    )
    api_key: str = Field(max_length=255)
    model_name: str = Field(max_length=100)
    capabilities: str = Field(
        default="text",
        max_length=100,
        description="能力标记，逗号分隔，取值 text / vision / embedding",
    )
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    # 只有 admin 能建公共提供商；非 admin 传 true 会被路由层拒（400）
    is_public: bool = False

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: str) -> str:
        return _normalize_capabilities(value)


class ProviderUpdate(BaseModel):
    """更新提供商 —— 所有字段可选，只改传了的字段"""

    # 用 Optional 让前端可以只填要改的字段（PATCH 语义）
    name: str | None = Field(default=None, max_length=50)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=255)
    model_name: str | None = Field(default=None, max_length=100)
    capabilities: str | None = Field(default=None, max_length=100)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    is_active: bool | None = None
    # 公共标记：非 admin 传了会被路由层拒（400）；这里只声明字段形态
    is_public: bool | None = None

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: str | None) -> str | None:
        return value if value is None else _normalize_capabilities(value)


class ProviderResponse(BaseModel):
    """对外响应（屏蔽 api_key 敏感字段——只返回掩码后的）"""

    id: int
    name: str
    base_url: str
    # 不返回真实 api_key，返回前 4 位 + ****
    api_key_masked: str
    model_name: str
    capabilities: str
    max_tokens: int
    temperature: float
    is_active: bool
    # 公共（官方默认）标记，仅 admin 可改；被授权用户列表里可见（SPEC §9.11）
    is_public: bool = False
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
            capabilities=obj.capabilities,
            max_tokens=obj.max_tokens,
            temperature=obj.temperature,
            is_active=obj.is_active,
            is_public=obj.is_public,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )
