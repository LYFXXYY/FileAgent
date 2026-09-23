"""一次性迁移：给 model_providers 增加 user_id 列（按用户隔离提供商）。

create_all() 只会建新表、不会 ALTER 已存在的表，所以手动迁移。
现有 dev 数据全部回填给真实账号 LYF(id=8)——若不存在则回填给最小 id 的用户。
幂等：重复运行会跳过已存在的列/约束。
"""

from sqlalchemy import text

import database

BACKFILL_USERNAME = "LYF"


def run() -> None:
    with database.engine.begin() as c:
        cols = [r[0] for r in c.execute(text("SHOW COLUMNS FROM model_providers")).fetchall()]
        if "user_id" in cols:
            print("[skip] user_id 列已存在")
            return

        # 决定回填目标 user_id
        row = c.execute(
            text("SELECT id FROM users WHERE username = :u ORDER BY id LIMIT 1"),
            {"u": BACKFILL_USERNAME},
        ).first()
        if row is None:
            row = c.execute(text("SELECT MIN(id) FROM users")).first()
        target_user_id = row[0] if row and row[0] is not None else None
        if target_user_id is None:
            raise SystemExit("users 表为空，无法回填 user_id；请先创建一个用户")

        c.execute(text("ALTER TABLE model_providers ADD COLUMN user_id INT NULL AFTER id"))
        c.execute(
            text("UPDATE model_providers SET user_id = :uid WHERE user_id IS NULL"),
            {"uid": target_user_id},
        )
        c.execute(text("ALTER TABLE model_providers MODIFY user_id INT NOT NULL"))
        c.execute(
            text(
                "ALTER TABLE model_providers "
                "ADD CONSTRAINT fk_model_providers_user "
                "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
            )
        )
        c.execute(text("CREATE INDEX ix_model_providers_user_id ON model_providers (user_id)"))
        print(f"[done] model_providers.user_id 已添加，现有行回填为 user_id={target_user_id}")


if __name__ == "__main__":
    run()
