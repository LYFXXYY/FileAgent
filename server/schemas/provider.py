"""模型提供商相关的请求/响应模型"""

import ipaddress
import socket
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from config import get_settings
from services.secrets import mask_secret

# 能力标记的合法取值。加新能力（如 audio）时改这里和前端下拉框即可
KNOWN_CAPABILITIES = ("text", "vision", "embedding")


def _validate_base_url(value: str) -> str:
    """
    校验 provider 的 base_url，挡掉 SSRF

    【为什么必须校】base_url 不是给用户浏览器打开的链接，而是**由服务端发出 POST**
    的目标（`services/llm.py` 拼 `{base_url}/chat/completions`）。任何登录用户填
    `http://169.254.169.254/latest/meta-data/` 就能让服务器替他去够云厂商的元数据服务，
    够 `http://127.0.0.1:5434` 就是在探本机数据库。挂在云上前这条必须堵。

    【为什么解析成 IP 再判，而不是字符串黑名单】`localhost`、`127.0.0.1` 这种写法
    太好绕：`127.1`、`0177.0.0.1`、`[::1]`、`127.0.0.1.nip.io`（公共 DNS 会解析回
    环回）都能骗过前缀匹配。所以先 `getaddrinfo` 拿到真实地址，再逐个判性质。

    【已知边界】校验时解析到的 IP 与真正发起请求时可能不同（DNS rebinding），
    彻底做法要在 httpx 的连接层再判一次；单机自用阶段先做到"默认拒绝内网"这一步。
    """
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    host = parsed.hostname
    if not host:
        raise ValueError("base_url 缺少主机名，例：https://api.example.com/v1")

    # 开发机要连本地假端点 / LM Studio 时才开这个开关；默认关闭（= 拒绝内网）
    if get_settings().allow_private_provider_urls:
        return raw

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (ValueError, socket.gaierror) as e:
        raise ValueError(f"base_url 的主机名不可用：{host}（{e}）") from e

    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"base_url 解析到内网或保留地址（{ip}），不允许。"
                "开发机确需连本地端点时，在 .env 里设 ALLOW_PRIVATE_PROVIDER_URLS=true"
            )
    return raw


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

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _validate_base_url(value)


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

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        # 编辑时可以不动 base_url（None = 没改），但**改了就必须重过一遍 SSRF 校验**，
        # 否则"建号时合规、编辑时改成内网地址"就把这道闸绕过去了
        return value if value is None else _validate_base_url(value)


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
            api_key_masked=mask_secret(obj.api_key),
            model_name=obj.model_name,
            capabilities=obj.capabilities,
            max_tokens=obj.max_tokens,
            temperature=obj.temperature,
            is_active=obj.is_active,
            is_public=obj.is_public,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )
