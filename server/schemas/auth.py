"""
认证相关的 Pydantic 模型

【Java 对比】
- 类似 @RequestBody DTO + @ResponseBody VO
- Pydantic 自动做输入校验（长度、正则、类型），非法请求直接返回 422
"""

from pydantic import BaseModel, Field, field_validator


def _reject_weak_password_value(value: str) -> str:
    """
    拒绝"整串只有一个字符类"的口令

    为什么不是复杂度那套（必须含大小写+数字+符号）：那种规则逼出 `Password123!`，
    既难记又是实际最常见的弱口令模板。这里只挡真正的一眼穿——纯数字（生日、
    手机号）和纯字母——把长度下限提到 8 才是主要防线。

    **注册与自助改密共用这一个函数**：两处各写一份迟早会漂，漂了就是"注册挡住的弱口令
    能从改密口子设进去"。登录不校验长度，老账号不会被这次改动锁在门外。
    """
    if value.isdigit():
        raise ValueError("密码不能只用数字")
    if value.isalpha():
        raise ValueError("密码不能只用字母，请混入数字或符号")
    return value


class RegisterRequest(BaseModel):
    """注册请求体"""

    # Field 定义约束，FastAPI 校验失败自动返回清晰错误
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\u4e00-\u9fa5]+$")
    # bcrypt 只处理 <= 72 字节，这里同步限制避免用户输入更长而按钮没反应
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def _reject_weak_password(cls, value: str) -> str:
        return _reject_weak_password_value(value)


class ChangePasswordRequest(BaseModel):
    """
    自助改密请求体

    原密码**不做强度校验**：它可能就是 6 位的历史口令，校验它等于把人挡在门外改不了。
    长度上限仍跟 bcrypt 的 72 字节走，否则超长输入会在 hash 处被静默截断。
    """

    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def _reject_weak_new_password(cls, value: str) -> str:
        return _reject_weak_password_value(value)


class LoginRequest(BaseModel):
    """登录请求体"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """登录成功返回的 JWT + 身份标记

    role / can_use_default 随登录一起下发，供前端首屏展示；启动时 /auth/me
    刷新一次快照，管理员改授权后无需重登。
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
