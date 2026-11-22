"""一次性迁移：model_providers.api_key 从明文改为 Fernet 密文，并把列宽 255 → 512。

为什么要有这个脚本：项目没有 Alembic，`create_all()` 只建新表、绝不 ALTER 已有表，
也不会改列宽（AGENTS §5）。而且**存量行必须加密**——只改代码不迁数据的话，
旧行仍是明文，而新的 `decrypt_secret` 严格失败（不退回明文），那些 provider 会直接用不了。

⚠ 安全前提：脚本先区分"解不开的两种原因"。
- 值**根本不像 Fernet 密文** → 判定为历史明文，加密它；
- 值**像 Fernet 密文但解不开** → 说明 `API_KEY_ENC_KEY` 与写入时不是同一把。
  这种情况**立即中止、一行都不改**：把别人的密文当明文再加密一层，等于永久毁掉它。

三种跑法：
    cd server && uv run python migrate_encrypt_api_keys.py --check     # 只报现状，不写
    cd server && uv run python migrate_encrypt_api_keys.py --dry-run   # 演练，不写
    cd server && uv run python migrate_encrypt_api_keys.py             # 真迁移

回滚：密文无法"逆向回明文"以外的方式撤销；要退回旧行为就把代码里的
`decrypt_secret` 调用去掉（不建议），或用备份的库快照恢复。
跑之前建议先 pg_dump 一次。
"""

import base64
import sys

from cryptography.fernet import Fernet
from sqlalchemy import select, text

import database
from services.secrets import encrypt_secret

TABLE = "model_providers"
WANTED_LEN = 512


def _looks_like_fernet(value: str) -> bool:
    """
    Fernet token 是 urlsafe base64，且第 1 字节是版本号 0x80 → base64 开头恒为 'gAAAAA'

    只用前缀 + 能否 base64 解码来判定，不做密码学校验：够区分"我们写的密文"与
    "用户当年的明文密钥"了（明文 API key 不会以 gAAAAA 开头且长度对不上）。
    """
    if not value or not value.startswith("gAAAAA") or len(value) < 60:
        return False
    try:
        raw = base64.urlsafe_b64decode(value.encode() + b"=" * (-len(value) % 4))
    except Exception:
        return False
    return len(raw) >= 57  # 1 版本 + 8 时间戳 + 16 IV + 16 HMAC，密文至少 41 字节


def _decryptable(value: str) -> bool:
    from config import get_settings

    key = (get_settings().api_key_enc_key or "").strip()
    if not key:
        raise SystemExit("未配置 API_KEY_ENC_KEY，无法迁移。先在 .env 里生成密钥再跑本脚本")
    try:
        Fernet(key.encode()).decrypt(value.encode())
        return True
    except Exception:
        return False


def _column_len(conn) -> int | None:
    return conn.execute(
        text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'api_key'"
        ),
        {"t": TABLE},
    ).scalar()


def run(check_only: bool, dry_run: bool) -> int:
    from models.provider import ModelProvider

    with database.engine.begin() as conn:
        cur_len = _column_len(conn)
        rows = conn.execute(
            select(ModelProvider.id, ModelProvider.name, ModelProvider.api_key)
        ).all()

        plaintext = [r for r in rows if not _looks_like_fernet(r.api_key or "")]
        # 像密文却解不开 = 密钥不对，动它就是毁数据
        suspect = [
            r for r in rows if _looks_like_fernet(r.api_key or "") and not _decryptable(r.api_key)
        ]
        print(
            f"[现状] {len(rows)} 个提供商：待加密明文 {len(plaintext)}、"
            f"已加密 {len(rows) - len(plaintext) - len(suspect)}、"
            f"密文但解不开 {len(suspect)}；api_key 列宽 {cur_len}"
        )

        if suspect:
            print(
                "[中止] 下面的行像密文却解不开，说明 API_KEY_ENC_KEY 与写入时不是同一把。"
                "继续会把密文再加密一层、永久毁掉它：\n"
                + "\n".join(f"  - id={r.id} name={r.name}" for r in suspect)
            )
            return 1

        if check_only:
            return 0

        if cur_len is not None and cur_len < WANTED_LEN:
            if dry_run:
                print(f"[dry-run] 将把 {TABLE}.api_key 放宽到 VARCHAR({WANTED_LEN})")
            else:
                conn.execute(
                    text(f"ALTER TABLE {TABLE} ALTER COLUMN api_key TYPE VARCHAR({WANTED_LEN})")
                )
                print(f"[done] {TABLE}.api_key 已放宽到 VARCHAR({WANTED_LEN})")

        for r in plaintext:
            token = encrypt_secret(r.api_key or "")
            if dry_run:
                print(f"[dry-run] 将加密 id={r.id} name={r.name}（{len(token)} 字符）")
                continue
            conn.execute(
                text(f"UPDATE {TABLE} SET api_key = :v WHERE id = :i"),
                {"v": token, "i": r.id},
            )
        print(
            f"[{'dry-run' if dry_run else 'done'}] 已加密 {len(plaintext)} 行；"
            "之后所有读取都走 services/secrets.decrypt_secret"
        )
        return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    sys.exit(run(check_only="--check" in args, dry_run="--dry-run" in args))
