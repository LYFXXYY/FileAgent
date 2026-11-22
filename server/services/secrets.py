"""
`provider.api_key` 的字段级加解密（Fernet 对称加密）

【为什么必须加密存储】
响应侧一直有脱敏（`api_key_masked`，前 4 位 + `****`），但**库里存的是明文**。
只要数据库文件、备份、或一条 `SELECT *` 的日志外泄，所有密钥就直接可用。
项目开放给别人用之后，库里躺的不再只是你自己的 key。

【设计取舍】
- **对称加密而不是"只存哈希"**：外呼时必须还原出原文拼 `Authorization` 头，哈希不可逆。
- **没有信封加密/KMS**：那是多人服务该做的事，这里是一把 env 里的 Fernet 密钥。
  代价明说：`API_KEY_ENC_KEY` **一旦丢失，库里所有密钥不可恢复**，必须单独备份它。
- **严格失败，不静默退回明文**：`decrypt_secret` 解不开就抛 `SecretError`。
  如果为"兼容性"加一条"看着不像密文就当明文用"，那攻击者只要往库里写一段明文
  就能让代码按明文处理——这道闸就形同虚设。未迁移的旧行走 `migrate_encrypt_api_keys.py`。
- 展示路径（列表页脱敏）**绝不因密钥问题报错**，见 `mask_secret`。
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from config import get_settings


class SecretError(RuntimeError):
    """密钥缺失、密文不合法、或密钥与密文不匹配"""


def generate_key() -> str:
    """生成一把 Fernet 密钥（填进 .env 的 API_KEY_ENC_KEY）"""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    """
    按当前配置构造 Fernet 实例。

    不做进程内缓存：`config.get_settings()` 本身已是 lru_cache，且测试会就地改
    配置对象，缓存住实例会让"换密钥"这件事在测试里失效。构造成本是微秒级。
    """
    key = (get_settings().api_key_enc_key or "").strip()
    if not key:
        raise SecretError(
            "未配置 API_KEY_ENC_KEY，无法读写模型密钥。"
            '生成：python -c "from services.secrets import generate_key; print(generate_key())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretError(
            "API_KEY_ENC_KEY 不是合法的 Fernet 密钥（应为 urlsafe base64 的 32 字节）"
        ) from e


def encrypt_secret(plaintext: str) -> str:
    """把明文密钥加密成可入库的字符串。空串也照样加密，避免"空=未加密"的歧义分支"""
    return _fernet().encrypt((plaintext or "").encode()).decode()


def decrypt_secret(stored: str) -> str:
    """
    还原文密里的密钥。解不开一律抛，不猜、不退化

    报错文案要直接指出两种可能（密钥不匹配 / 该行还没迁移），否则运维只能瞎猜。
    """
    if not stored:
        return ""
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as e:
        raise SecretError(
            "模型密钥解密失败：API_KEY_ENC_KEY 与写入时不是同一把，"
            "或该行仍是未迁移的明文（跑 migrate_encrypt_api_keys.py）"
        ) from e


def mask_secret(stored: str) -> str:
    """
    列表页展示的掩码

    解得开就露前 4 位（沿用原来的可读性约定），解不开返回统一掩码而不是抛错——
    这是展示路径，因为密钥问题让「模型」页整个 500 是最坏的用户体验，
    真正需要报错的是外呼路径（`decrypt_secret`）。
    """
    try:
        plain = decrypt_secret(stored)
    except SecretError:
        return "****"
    return f"{plain[:4]}****" if len(plain) > 4 else "****"
