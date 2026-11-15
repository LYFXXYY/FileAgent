"""一次性迁移：给 operation_logs 增加 structured_output 列（结构化批改的问题清单）。

create_all() 只建新表、不会 ALTER 已存在的表，所以新列必须手动迁移。
幂等：列已存在直接跳过，可重复运行。新列全部留 NULL，历史日志无需回填
（NULL 就表示"这条没有结构化清单"，前端据此不显示问题卡片）。
"""

from sqlalchemy import text

import database


def run() -> None:
    with database.engine.begin() as c:
        cols = [r[0] for r in c.execute(text("SHOW COLUMNS FROM operation_logs")).fetchall()]
        if "structured_output" in cols:
            print("[skip] structured_output 列已存在")
            return

        # 放在 processed_content 后面，读表时两个正文字段挨着
        c.execute(
            text(
                "ALTER TABLE operation_logs "
                "ADD COLUMN structured_output TEXT NULL AFTER processed_content"
            )
        )
        print("[done] operation_logs.structured_output 已添加，历史行保持 NULL")


if __name__ == "__main__":
    run()
