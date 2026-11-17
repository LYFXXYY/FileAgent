"""一次性迁移：管理员 + 默认模型授权的三列 + bootstrap 首个 admin（SPEC §9.11）。

create_all() 不 ALTER 已有表，新列必须手动加（Postgres 方言，ADD COLUMN IF NOT EXISTS 幂等）。
bootstrap：把指定用户名的账号升为 admin（默认 LYF，可传参覆盖）。**只提升、不降级**，
重复执行安全。

跑法：cd server && uv run python migrate_add_admin_support.py [用户名]
"""

import sys

from sqlalchemy import text

import database

DEFAULT_BOOTSTRAP_ADMIN = "LYF"


def run(username: str = DEFAULT_BOOTSTRAP_ADMIN) -> None:
    with database.engine.begin() as c:
        c.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(10) "
                "NOT NULL DEFAULT 'user'"
            )
        )
        c.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_use_default BOOLEAN "
                "NOT NULL DEFAULT false"
            )
        )
        c.execute(
            text(
                "ALTER TABLE model_providers ADD COLUMN IF NOT EXISTS is_public BOOLEAN "
                "NOT NULL DEFAULT false"
            )
        )
        print("[done] 三列就位（users.role / users.can_use_default / model_providers.is_public）")

        result = c.execute(text("UPDATE users SET role='admin' WHERE username=:u"), {"u": username})
        if result.rowcount:
            print(f"[done] 已把 {username} 升级为 admin")
        admin_count = c.execute(text("SELECT COUNT(*) FROM users WHERE role='admin'")).scalar() or 0
        if admin_count == 0:
            print(
                f"[warn] 系统里没有任何 admin（用户 {username} 不存在？）。"
                f"先注册该账号再重跑本脚本，或手动 UPDATE users SET role='admin'。"
            )
        else:
            print(f"[info] 当前 admin 数：{admin_count}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BOOTSTRAP_ADMIN)
