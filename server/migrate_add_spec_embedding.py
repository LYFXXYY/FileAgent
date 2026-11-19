"""一次性迁移：spec_chunks 加 embedding 列（阶段 6 前置约束 6）。

为什么要有这个脚本：项目没有 Alembic，启动只跑 `create_all()`，它**只建新表、绝不 ALTER
已有表**。所以新列必须手写幂等迁移（AGENTS.md §5）。Postgres 的
`ADD COLUMN IF NOT EXISTS` 天然幂等，重复跑安全。

两种跑法：
    cd server && uv run python migrate_add_spec_embedding.py --check   # 只看现状，不写
    cd server && uv run python migrate_add_spec_embedding.py           # 真加列

⚠ 本脚本**故意不建 ANN 索引**（HNSW / ivfflat）。前置约束 #1 实测：pgvector 的近似索引
带 `user_id` 过滤会静默少返回 top-k，而 §3.1 的数据隔离要求每个查询都带 user_id——
两者正好对撞。小库阶段先走精确扫描，等量级到万级再连同过滤策略一起重审。

回滚（列是 nullable，删掉不影响已有数据）：
    docker exec fileagent-postgres psql -U fileagent -d file_agent \\
      -c "ALTER TABLE spec_chunks DROP COLUMN embedding;"
"""

import sys

from sqlalchemy import text

import database

# 与 LM Studio 上 bge-m3 的实测输出维度一致。换模型必须同步改这里并重建列，
# 否则向量维度对不上会在写入时才炸（前置约束：模型名要写死并校验维度）
EMBEDDING_DIM = 1024


def _vector_ready(conn) -> bool:
    return (
        conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
        is not None
    )


def _column_state(conn) -> dict:
    """
    读列的存在性与真实维度。

    ⚠ 不能用 `atttypid::regtype::text` 判维度——pgvector 的 vector 经 regtype 只渲染成
    "vector"，维度存在 atttypmod 里（-1 表示未限定）。第一版拿字符串比 "vector(1024)"，
    结果每次重跑都误报"维度不一致"。
    """
    row = (
        conn.execute(
            text(
                "SELECT a.atttypmod AS typmod, t.typname AS base "
                "FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid "
                "WHERE a.attrelid = 'spec_chunks'::regclass AND a.attname = 'embedding'"
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"exists": False, "dim": None, "base": None}
    dim = row["typmod"] if row["typmod"] is not None and row["typmod"] > 0 else None
    return {"exists": True, "base": row["base"], "dim": dim}


def _filled(conn) -> int:
    return conn.execute(
        text("SELECT COUNT(*) FROM spec_chunks WHERE embedding IS NOT NULL")
    ).scalar()


def run(check_only: bool) -> int:
    with database.engine.begin() as conn:
        if not _vector_ready(conn):
            print(
                "[abort] 库里没有 vector 扩展。镜像是 pgvector/pgvector:pg16，"
                "正常应自带；先确认连的是 fileagent-postgres 而不是别的 Postgres。"
            )
            return 1

        state = _column_state(conn)
        total = conn.execute(text("SELECT COUNT(*) FROM spec_chunks")).scalar()

        if state["exists"]:
            print(
                f"[现状] embedding 已存在：类型 {state['base']}"
                f"({state['dim'] or '未限定'} 维)，"
                f"已回填 {total and _filled(conn)}/{total} 块"
            )
            if state["dim"] != EMBEDDING_DIM:
                print(
                    f"[warn] 现有维度不是 {EMBEDDING_DIM}。换 embedding 模型时维度必须一致，"
                    "否则旧向量会和新向量混在同一列里，检索结果不可解释。"
                )
                print("       修法：删列重加（规范库不可变，删了重传比原地改安全）。")
            return 0

        if check_only:
            print("[check] 将执行：ALTER TABLE spec_chunks ADD COLUMN")
            print(f"        embedding vector({EMBEDDING_DIM})；现有 {total} 块先变 NULL 等回填。")
            return 0

        conn.execute(
            text(
                f"ALTER TABLE spec_chunks ADD COLUMN IF NOT EXISTS "
                f"embedding vector({EMBEDDING_DIM})"
            )
        )
        after = _column_state(conn)
        print(f"[done] spec_chunks.embedding 已加：{after['type']}，现有 {total} 块全为 NULL")
        print("       未建 ANN 索引（前置约束 #1）；回填向量走 services 侧批量编码。")
        return 0


if __name__ == "__main__":
    sys.exit(run(check_only="--check" in sys.argv[1:]))
