"""回填规范库向量：把 spec_chunks 里 embedding IS NULL 的块补齐（阶段 6 A1）

为什么需要它：加列的迁移（migrate_add_spec_embedding.py）只 ALTER 不回填，
历史块全是 NULL；另外 `SPEC_EMBED_ON_INGEST=false` 期间入库的文档也只有文本。
这两种 NULL 不是"这条规范不该有向量"，而是"还没算过"——检索会把它们整体排除，
所以回填前那部分条款等于不在库里。

两种跑法：
    cd server && uv run python backfill_spec_embeddings.py --check   # 只看还欠多少
    cd server && uv run python backfill_spec_embeddings.py           # 真编码并写回

实测 66 条一批 2.17s，比逐条快 1.6 倍，所以这里直接批量发、不做异步任务体系。
脚本可重复跑：每轮只挑 NULL，写完就没了。

⚠ 只碰 embedding 这一列，不改文本、不改块边界（规范库不可变，要改内容就重传）。
"""

from __future__ import annotations

import sys

from sqlalchemy import func, select

import database
from models.spec_doc import SpecChunk
from services.embedding import EmbeddingError, embed_texts


def run(check_only: bool) -> int:
    db = database.SessionLocal()
    try:
        total = db.scalar(select(func.count(SpecChunk.id))) or 0
        pending = db.scalar(select(func.count(SpecChunk.id)).where(SpecChunk.embedding.is_(None)))
        pending = pending or 0

        print(f"[现状] spec_chunks 共 {total} 块，待回填 {pending} 块")
        if pending == 0:
            print("[skip] 全部块都已有向量，无需回填")
            return 0

        if check_only:
            chars = db.scalar(
                select(func.sum(func.length(SpecChunk.text))).where(SpecChunk.embedding.is_(None))
            )
            print(f"[check] 将编码 {pending} 块 / 约 {chars or 0} 字")
            print("        字数是成本代理：该端点 usage.prompt_tokens 恒 0，拿不到真实 token")
            return 0

        rows = (
            db.execute(
                select(SpecChunk).where(SpecChunk.embedding.is_(None)).order_by(SpecChunk.id)
            )
            .scalars()
            .all()
        )
        try:
            vectors = embed_texts([r.text for r in rows])
        except EmbeddingError as e:
            print(f"[abort] {e}")
            print("        编码失败不落任何半成品：要么整批补齐，要么一块不动。")
            return 1

        for row, vec in zip(rows, vectors, strict=True):
            row.embedding = vec
        db.commit()

        left = db.scalar(select(func.count(SpecChunk.id)).where(SpecChunk.embedding.is_(None)))
        print(f"[done] 已写回 {len(rows)} 块，剩余 NULL {left or 0} 块")
        print("       未建 ANN 索引（前置约束 #1）：小库走精确余弦扫描。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    # Windows GBK 控制台会把中文打成问号（AGENTS §3.5）
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run(check_only="--check" in sys.argv[1:]))
