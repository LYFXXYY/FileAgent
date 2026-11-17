"""
认证相关的 Pydantic 模型

【Java 对比】
- 类似 @RequestBody DTO + @ResponseBody VO
- Pydantic 自动做输入校验（长度、正则、类型），非法请求直接返回 422
"""

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """注册请求体"""

    # Field 定义约束，FastAPI 校验失败自动返回清晰错误
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\u4e00-\u9fa5]+$")
    # bcrypt 只处理 <= 72 字节，这里同步限制避免用户输入更长按钮没反应
    password: str = Field(min_length=6, max_length=72)


class LoginRequest(BaseModel):
    """登录请求体"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """登录成功返回的 JWT + 身份标记

    role / can_use_default 随登录一起下发：前端靠 role 决定要不要显示管理入口，
    靠 can_use_default 决定 Providers 页是否展示"官方"条目——不用再开 /auth/me。
    """

    access_token: str
    token_type: str = "bearer"
    username: str
    role: str = "user"
    can_use_default: bool = False


class UserResponse(BaseModel):
    """对外暴露的用户信息（绝不返回 password_hash）"""

    id: int
    username: str
    role: str = "user"
    can_use_default: bool = False

    # Pydantic v2 语法：允许从 ORM 对象直接构造响应
    # 类比 Java 的 ModelMapper.map(entity, UserResponse.class)
    model_config = {"from_attributes": True}
