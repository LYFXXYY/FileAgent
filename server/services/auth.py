"""
认证服务 —— 密码哈希 + JWT 签发/验证

【Java 对比】
- hash_password ≈ BCryptPasswordEncoder.encode()
- verify_password ≈ passwordEncoder.matches(raw, hashed)
- create_access_token ≈ Jwts.builder()...signWith(key).compact()

【为什么不用 passlib？】
- passlib 1.7.4（2020 年停更）与 bcrypt 4.1+ 不兼容：
  passlib 内部会用一个 5120 字节密码探测 bcrypt 版本，但新版 bcrypt 直接拒绝 > 72 字节的输入
- 社区共识：直接用 bcrypt 库更清爽，反正我们只用一种算法
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from config import get_settings

settings = get_settings()

# bcrypt 硬编码限制：输入必须 <= 72 字节（协议设计如此）
# 若未来要支持更长的密码/passphrase，标准做法是先 SHA-256 摘要 → 再 bcrypt
BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    """把 str 转成 UTF-8 字节并截到 72 字节，避免 bcrypt 报错"""
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > BCRYPT_MAX_BYTES:
        # 截断而不是抛错——上层已经在 schema 里限了 max_length=72
        # 这里是双重保险
        pw_bytes = pw_bytes[:BCRYPT_MAX_BYTES]
    return pw_bytes


def hash_password(password: str) -> str:
    """明文密码 → bcrypt 哈希（每次结果都不同，因为内部随机 salt）"""
    hashed = bcrypt.hashpw(_prepare(password), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证明文和哈希是否匹配（不能直接 hash 比对，要用库的 checkpw）

    bcrypt 会自动从哈希中提取 salt 重算，比对结果
    """
    try:
        return bcrypt.checkpw(_prepare(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        # 哈希格式非法（如数据库被写坏）→ 视为验证失败
        return False


# ---------- JWT ----------
# JWT 由三段组成：header.payload.signature，用 HMAC-SHA256 签名
# 类比 Java 的 io.jsonwebtoken.Jwts 库
JWT_ALGORITHM = "HS256"


def create_access_token(user_id: int, username: str) -> str:
    """
    签发 JWT access_token

    payload 字段：
      sub      → subject，存用户 ID（字符串），JWT 标准字段
      username → 冗余存用户名，方便前端直接展示
      exp      → 过期时间戳（Unix seconds），过期后 decode 会自动失败
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": expire,
    }
    # jwt.encode 内部完成 JSON 序列化 + base64url + HMAC 签名
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """
    验证并解析 JWT，返回 payload；无效或过期返回 None

    不抛异常而是返回 None：让调用方决定如何处理（401 或走其他逻辑）
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        # 包括：签名不匹配、token 过期、格式错误等
        return None
