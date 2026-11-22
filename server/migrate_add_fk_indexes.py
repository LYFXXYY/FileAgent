"""一次性迁移：给两个外键列补索引（删任务 / 删提供商的 UPDATE 走全表扫描）。

为什么要有这个脚本：项目没有 Alembic，启动只跑 `create_all()`，它**只建新表、绝不 ALTER
已有表**，也不会给已存在的表补建后加的索引。所以索引变更必须手写幂迁移（AGENTS.md §5）。

为什么是这两列：删除路径要先置空引用再删（§3.2 的硬顺序）——
- `api/v1/tasks.py:delete_task` 跑 `UPDATE operation_logs SET task_id = NULL WHERE task_id = ?`
- `api/v1/providers.py:delete_provider` 按 `provider_id` 做同样的置空
两条都是按外键列过滤的全表 UPDATE，没索引就是顺序扫描。日志表随使用只增不减，
这份代价是每天都付的。

索引名沿用 SQLAlchemy `index=True` 的默认命名 `ix_<表>_<列>`，这样新库 `create_all()`
建出来的名字与本脚本逐字一致，不会留下"两种库索引名不同"的差异。

两种跑法：
    cd server && uv run python migrate_add_fk_indexes.py --check   # 只看现状，不写
    cd server && uv run python migrate_add_fk_indexes.py           # 真建索引

回滚（两条 DROP INDEX IF EXISTS，分别针对 operation_logs.task_id 与 tasks.provider_id）。
"""

import sys

from sqlalchemy import text

import database

# 索引名 -> 建索引的表.列
_INDEXES = {
    "ix_operation_logs_task_id": "operation_logs(task_id)",
    "ix_tasks_provider_id": "tasks(provider_id)",
}


def _existing(conn) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = ANY(:names)"
        ),
        {"names": list(_INDEXES)},
    ).scalars()
    return set(rows)


def run(check_only: bool) -> int:
    with database.engine.begin() as conn:
        have = _existing(conn)
        missing = [name for name in _INDEXES if name not in have]

        if not missing:
            print("[现状] 两个索引都已存在：" + "、".join(_INDEXES))
            return 0

        if check_only:
            print(f"[check] 将建索引：{', '.join(missing)}")
            return 0

        for name in missing:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {_INDEXES[name]}"))
        print(f"[done] 已建索引：{', '.join(missing)}")
        return 0


if __name__ == "__main__":
    # Windows GBK 控制台直接 print 中文会乱码（AGENTS §3.5）
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run(check_only="--check" in sys.argv[1:]))
