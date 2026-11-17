"""一次性迁移：tasks 表加 output_format 列（阶段 5 s5.1）。

create_all() 只建新表、绝不 ALTER 已有表，所以新列必须手动加。
Postgres 方言：ADD COLUMN IF NOT EXISTS 自带幂等。

跑法：cd server && uv run python migrate_add_task_output_format.py
"""

from sqlalchemy import text

import database

DEFAULT_FORMAT = "docx"


def run() -> None:
    with database.engine.begin() as c:
        # 用 IF NOT EXISTS 替代 MySQL 时代 SHOW COLUMNS 判存的老套路
        c.execute(
            text(
                "ALTER TABLE tasks "
                "ADD COLUMN IF NOT EXISTS output_format VARCHAR(10) "
                f"NOT NULL DEFAULT '{DEFAULT_FORMAT}'"
            )
        )
        # 检查现有行有没有没回填到的（理论不会，但留个保险）
        null_count = c.execute(
            text("SELECT COUNT(*) FROM tasks WHERE output_format IS NULL")
        ).scalar()
        print(f"[done] tasks.output_format 已就位，默认 {DEFAULT_FORMAT}；NULL 行 {null_count}")


if __name__ == "__main__":
    run()
