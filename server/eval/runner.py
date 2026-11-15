"""
评测台 CLI：跑一轮 golden set，落一份可对比的 run JSON

用法（都在 server 目录下）：

    uv run python -m eval.runner --provider 5                  # 跑全部夹具
    uv run python -m eval.runner --provider 5 --judge          # 额外要一个 1–5 主观分
    uv run python -m eval.runner --provider 5 --prompt-file cand.txt --label 候选A
    uv run python -m eval.runner --provider 5 --baseline eval/runs/xxx.json   # 与上轮对拍

⚠ 会产生真实 token 花费（每条夹具一次批改，--judge 每条再多一次）。
   先用 `--case` 单跑一条把链路验通，再决定要不要跑全量。

这是**运维脚本**，不是接口：按 provider 主键直取，不做 user_id 隔离。改它时不要把
"每个查询都带 user_id"当漏掉的 bug —— 那条约束管的是对外路由，本机单租户工具走另
一套口径（见 AGENTS.md §3.1）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from eval.cases import CASES, GoldenCase, case_by_id
from eval.score import RATIO_METRICS, aggregate, diff_runs, score_case
from models.provider import ModelProvider
from schemas.grading import GRADING_JSON_SCHEMA
from services.grading import build_messages, parse_grading_output
from services.llm import LLMError, chat_json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")

# 裁判只要一个整数分、一份漏检清单、一句理由：字段越少，端点越不容易吐坏
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "missed": {"type": "array", "items": {"type": "string"}},
        "comment": {"type": "string"},
    },
    "required": ["score", "missed", "comment"],
    "additionalProperties": False,
}

JUDGE_INSTRUCTION = """你是严格的批改质量评审。下面给你一篇原文、模型的修订稿和模型的问题清单，\
请按三个维度综合判 1–5 分（5 最好）：
- 该指的问题是否都指出来了，有没有硬挑刺
- 修订稿是否真的把问题改掉了（原样退回最扣分）
- 建议是否可直接照做，没有画蛇添足

只输出 JSON，不要解释。score 为整数；missed 列出被漏掉的真问题，没有就给空数组；\
comment 用一句话说明扣分点。

【原文】
{doc}

【模型修订稿】
{revised}

【模型问题清单】
{issues}
"""


def _load_template(path: str | None) -> str | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if "{{content}}" not in text:
        raise SystemExit("提示词文件必须包含 {{content}} 占位符，否则文档进不去")
    return text


def _prompt_for(case: GoldenCase, template: str | None) -> str:
    """--prompt-file 整体替换批改指令，用来 A/B 候选提示词"""
    return (template or case.instruction).replace("{{content}}", case.doc)


async def _grade(provider: ModelProvider, case: GoldenCase, template: str | None) -> Any:
    """一次批改，返回 (分数 dict, GradingResult)；异常原样抛给调用方记账"""
    result, payload = await chat_json(
        build_messages(_prompt_for(case, template)), provider, json_schema=GRADING_JSON_SCHEMA
    )
    grading = parse_grading_output(result.content, payload, case.doc)
    scored = score_case(case, grading)
    scored.update(
        tier=result.format_tier,
        tokens=result.total_tokens,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        degraded=grading.degraded,
    )
    return scored, grading


async def _judge(provider: ModelProvider, case: GoldenCase, grading: Any) -> dict:
    """
    主观分是旁路：判分失败只记 error，绝不把已经跑完的客观指标带走

    和 `record_llm_call()` 同一个道理——观测手段不能变成故障源。
    """
    instruction = JUDGE_INSTRUCTION.format(
        doc=case.doc,
        revised=grading.revised_text or "（空）",
        issues=json.dumps([i.model_dump() for i in grading.issues], ensure_ascii=False),
    )
    try:
        result, payload = await chat_json(
            [{"role": "user", "content": instruction}], provider, json_schema=JUDGE_SCHEMA
        )
    except LLMError as e:
        return {"error": str(e)[:300]}
    if not isinstance(payload, dict):
        return {"error": "裁判输出不可解析", "raw": result.content[:200]}
    return {
        "score": payload.get("score"),
        "missed": payload.get("missed") or [],
        "comment": (payload.get("comment") or "")[:400],
        "tier": result.format_tier,
        "tokens": result.total_tokens,
    }


async def _run_cases(provider, cases, template, want_judge) -> tuple[list[dict], int]:
    per_case: list[dict] = []
    judge_tokens = 0
    for case in cases:
        try:
            scored, grading = await _grade(provider, case, template)
        except LLMError as e:
            per_case.append({"id": case.id, "name": case.name, "error": str(e)[:300]})
            print(f"  [FAIL] {case.id}: {str(e)[:120]}")
            continue

        if want_judge:
            judged = await _judge(provider, case, grading)
            scored["judge"] = judged
            judge_tokens += judged.get("tokens") or 0
        per_case.append(scored)
        brief = {k: scored.get(k) for k in (*RATIO_METRICS, "issues", "false_positives")}
        print(f"  [OK] {case.id:<12} tier={scored['tier']} {json.dumps(brief, ensure_ascii=False)}")
    return per_case, judge_tokens


async def run_all(args: argparse.Namespace) -> dict[str, Any]:
    import database

    template = _load_template(args.prompt_file)
    cases = [case_by_id(args.case)] if args.case else list(CASES)

    with database.SessionLocal() as db:
        provider = db.get(ModelProvider, args.provider)
        if provider is None:
            raise SystemExit(f"提供商 id={args.provider} 不存在")
        # 快照成普通值：session 一关，ORM 属性就过期了
        ref = {"id": provider.id, "name": provider.name, "model": provider.model_name}
        per_case, judge_tokens = await _run_cases(provider, cases, template, args.judge)

    scored_rows = [c for c in per_case if "issues" in c]
    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": ref,
        "label": args.label or ("prompt-file" if template else "builtin"),
        "prompt_digest": _digest(template or "|".join(c.instruction for c in cases)),
        "judge": bool(args.judge),
        "tokens": sum(c.get("tokens") or 0 for c in scored_rows) + judge_tokens,
        "cases": per_case,
        "aggregate": aggregate(scored_rows),
    }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _print_summary(report: dict[str, Any]) -> None:
    agg = report["aggregate"]
    total = agg["cases"]
    print("\n===== 汇总 =====")
    print(f"用例 {total}｜意见合计 {agg['issues_total']}｜误报 {agg['false_positives']}")
    for key in RATIO_METRICS:
        val = agg.get(key)
        print(f"  {key:<14} {'n/a' if val is None else f'{val:.4f}'}")
    print(f"  格式合规      {agg['format_ok_count']}/{total}")
    print(f"  条数达标      {agg['min_issues_ok_count']}/{total}")
    print(f"  token 合计    {report['tokens']}")


def _write_run(report: dict[str, Any], args: argparse.Namespace) -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = report["created_at"].replace(":", "").replace("-", "").replace("+", "")[:15]
    path = args.out or os.path.join(RUNS_DIR, f"{stamp}-{args.provider}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="批改质量评测台（阶段 2）")
    parser.add_argument("--provider", type=int, required=True, help="提供商 id（会真花钱）")
    parser.add_argument("--case", help="只跑这一条夹具的 id")
    parser.add_argument("--prompt-file", help="用文件内容整体替换批改指令，做 A/B")
    parser.add_argument("--label", help="本轮标记，写进 run JSON")
    parser.add_argument("--judge", action="store_true", help="额外调一次模型打 1–5 主观分")
    parser.add_argument("--baseline", help="与这一份 run JSON 对比")
    parser.add_argument("--out", help="run JSON 输出路径，默认 eval/runs/<时间戳>-<provider>.json")
    args = parser.parse_args()

    report = asyncio.run(run_all(args))
    _print_summary(report)

    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            baseline = json.load(f)
        print("\n===== 与 baseline 对比 =====")
        for line in diff_runs(baseline, report) or ["（指标无变化）"]:
            print(f"  {line}")

    print(f"\nrun 已写入 {_write_run(report, args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
