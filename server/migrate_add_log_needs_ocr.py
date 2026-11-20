"""一次性迁移：operation_logs 加 needs_ocr + stored_filename 两列（MinerU 扫描件兜底）。

为什么要有这个脚本：项目没有 Alembic，启动只跑 `create_all()`，它**只建新表、绝不 ALTER
已有表**。所以新列必须手写幂等迁移（AGENTS.md §5）。Postgres 的
`ADD COLUMN IF NOT EXISTS` 天然幂等，重复跑安全。

两种跑法：
    cd server && uv run python migrate_add_log_needs_ocr.py --check   # 只看现状，不写
    cd server && uv run python migrate_add_log_needs_ocr.py           # 真加列

语义：
- needs_ocr=true 表示"扫描件已受理、正文待 OCR"。历史行一律 false（默认值）。
- stored_filename 记录落盘名：后台 OCR 要靠它找回上传的 PDF。历史行 NULL
  （旧数据本来就不可回溯），新上传一律记录——顺带补上磁盘副本的审计缺口。
ORM 已同步声明两列，新库 create_all() 直接带。

回滚：
    docker exec fileagent-postgres psql -U fileagent -d file_agent \\
      -c "ALTER TABLE operation_logs DROP COLUMN needs_ocr, DROP COLUMN stored_filename;"
"""

import sys

from sqlalchemy import text

import database

_COLUMNS = {
    "needs_ocr": "BOOLEAN NOT NULL DEFAULT FALSE",
    "stored_filename": "VARCHAR(64)",
}


def _existing(conn) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'operation_logs' AND column_name = ANY(:names)"
        ),
        {"names": list(_COLUMNS)},
    ).scalars()
    return set(rows)


def run(check_only: bool) -> int:
    with database.engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM operation_logs")).scalar()
        have = _existing(conn)
        missing = [name for name in _COLUMNS if name not in have]

        if not missing:
            pending = conn.execute(
                text("SELECT COUNT(*) FROM operation_logs WHERE needs_ocr")
            ).scalar()
            print(f"[现状] 两列已存在：{total} 行日志，其中待 OCR {pending} 行")
            return 0

        if check_only:
            print(f"[check] 将加列：{', '.join(missing)}；现有 {total} 行")
            return 0

        for name in missing:
            conn.execute(
                text(f"ALTER TABLE operation_logs ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}")
            )
        print(f"[done] 已加列：{', '.join(missing)}；现有 {total} 行取默认值")
        return 0


if __name__ == "__main__":
    sys.exit(run(check_only="--check" in sys.argv[1:]))
