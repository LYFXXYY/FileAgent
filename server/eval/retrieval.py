"""检索命中率评测（阶段 6 A4）：recall@k，零真实 token

用法（都在 server 目录下）：

    uv run python -m eval.retrieval                        # 三种召回方式对拍
    uv run python -m eval.retrieval --modes hybrid --k 10   # 只看混合、换 k
    uv run python -m eval.retrieval --case de_di_de_01      # 单条夹具

和 `eval.runner` 的分工：那边量的是"模型批得好不好"（要花钱），这边量的是
"该给的条款有没有进 prompt"（只打本地 embedding，不花钱）。两层必须分开——
检索没命中和模型没采纳，在最终批改结果里长得一模一样。

⚠ **这批夹具结构性地偏向词法**：阶段 6 夹具的错例本来就是从那份规范里抄出来的，
所以字二元组重合度天然高。词法 recall 好看只说明"这一层信号在本库里可区分"，
不能当成"词法优于向量"的世界性结论。真正能泛化到用户自己文档的是向量那一路。

⚠ 这是运维脚本，按 user_id 直取自己的规范库，不做越权测试（AGENTS.md §3.1）。
   它连真 Postgres；vector / hybrid 两臂还要求本地 embedding 端点在跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

import database
from eval.cases import CASES, case_by_id
from models.spec_doc import SpecDoc
from services.spec_retrieval import SpecHit, retrieve_for_document

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
MODES = ("hybrid", "vector", "lexical")


def _metrics(expected: tuple[str, ...], got: list[str]) -> dict[str, Any]:
    """
    单条夹具的检索指标。

    夹具没声明 expect_clauses 时返回 None 而不是 0——写 0 会把平均分整体拉低，
    看起来"变差了"其实是自己造的假信号（§3.9 第一条）。
    """
    if not expected:
        return {"recall_at_k": None, "hit_at_k": None, "first_rank": None}
    hit = [c for c in got if c in expected]
    return {
        "recall_at_k": len(set(hit)) / len(expected),
        "hit_at_k": 1.0 if hit else 0.0,
        # 第一条期望条款排在第几位：比"有没有命中"更能说明注入预算够不够
        "first_rank": (got.index(hit[0]) + 1) if hit else None,
    }


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def run(user_id: int, k: int, modes: list[str], only_case: str | None) -> dict[str, Any]:
    db = database.SessionLocal()
    try:
        docs = db.execute(
            select(SpecDoc.id, SpecDoc.title, SpecDoc.chunk_count).where(SpecDoc.user_id == user_id)
        ).all()
        if not docs:
            raise SystemExit(
                f"user_id={user_id} 的规范库是空的。先上传规范文档，或用 --user 指到有的账号"
            )
        print(f"规范库：{[(d.id, d.title, d.chunk_count) for d in docs]}  k={k}  modes={modes}")

        cases = [case_by_id(only_case)] if only_case else [c for c in CASES if c.expect_clauses]
        rows: list[dict[str, Any]] = []
        for case in cases:
            row: dict[str, Any] = {"id": case.id, "expect": list(case.expect_clauses)}
            for mode in modes:
                hits: list[SpecHit] = retrieve_for_document(
                    db, user_id=user_id, document_text=case.doc, top_k=k, mode=mode
                )
                got = [h.clause for h in hits]
                row[mode] = {"got": got, **_metrics(case.expect_clauses, got)}
            rows.append(row)

            print(f"\n{case.id}  期望 {','.join(case.expect_clauses)}")
            for mode in modes:
                m = row[mode]
                print(
                    f"    {mode:<8} recall={_fmt(m['recall_at_k'])} "
                    f"hit={_fmt(m['hit_at_k'])} 首命中={m['first_rank']} "
                    f"top=[{', '.join(m['got'])}]"
                )

        per_mode = {
            mode: {
                "cases": len(rows),
                "recall_at_k": _mean([r[mode] for r in rows], "recall_at_k"),
                "hit_at_k": _mean([r[mode] for r in rows], "hit_at_k"),
            }
            for mode in modes
        }
        return {
            "kind": "retrieval",
            "user_id": user_id,
            "k": k,
            "modes": modes,
            "spec_docs": [{"id": d.id, "title": d.title, "chunks": d.chunk_count} for d in docs],
            "cases": rows,
            "aggregate": per_mode,
            "created_at": datetime.now(UTC).isoformat(),
        }
    finally:
        db.close()


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="规范库检索命中率（不花 token）")
    parser.add_argument("--user", type=int, default=None, help="规范库属主，默认取有库的那个账号")
    parser.add_argument("--k", type=int, default=None, help="top-k，默认走 SPEC_RETRIEVAL_TOP_K")
    parser.add_argument("--modes", default="hybrid,vector,lexical", help="逗号分隔的召回方式")
    parser.add_argument("--case", help="只跑这一条夹具 id")
    parser.add_argument("--out", help="run JSON 路径，默认 eval/runs/retrieval-<时间戳>.json")
    args = parser.parse_args()

    modes = [m.strip().lower() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if bad:
        print(f"[abort] 不支持的 mode {bad}，可选：{list(MODES)}")
        return 1

    db = database.SessionLocal()
    try:
        user_id = args.user
        if user_id is None:
            row = db.execute(select(SpecDoc.user_id).order_by(SpecDoc.id).limit(1)).first()
            if row is None:
                print("[abort] 库里没有任何规范文档")
                return 1
            user_id = row[0]
    finally:
        db.close()

    from config import get_settings

    k = args.k or get_settings().spec_retrieval_top_k

    report = run(user_id, k, modes, args.case)

    print(f"\n==== 汇总（{len(report['cases'])} 条夹具，k={k}）")
    for mode in modes:
        agg = report["aggregate"][mode]
        print(
            f"  {mode:<8} recall@{k} = {_fmt(agg['recall_at_k'])}   "
            f"hit@{k} = {_fmt(agg['hit_at_k'])}"
        )

    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.out or os.path.join(RUNS_DIR, f"retrieval-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nrun JSON → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
