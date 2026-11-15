"""
确定性打分 —— 把一次批改变成一组可对拍的比例

【为什么先要确定性指标】
LLM-as-judge 也还是要花钱、也还是会飘。这四个数字不要模型、不要标注复核、
跑一遍就是一个能进 CI 的门禁：定位率、意见命中率、修订落地率、误报数。
真机第一轮就靠"修订落地率"抓出了问题：意见 4 条全对，revised_text 却与原文
一字不差——只看 degraded 和条数的话，这个缺陷会一路漏到用户手里。

判不了的期望一律返回 None（不是 0），聚合时按"该用例不参与此指标"处理。
"""

from __future__ import annotations

from typing import Any

from eval.cases import GoldenCase
from schemas.grading import GradingResult

# 一条意见至少要有多少原文落在文档里才算"能定位"
_MIN_ISSUES_FOR_RATE = 1


def _located_ratio(grading: GradingResult) -> float | None:
    """意见里有多少条真的指到了原文位置。没有意见时无定义，别算成满分"""
    if len(grading.issues) < _MIN_ISSUES_FOR_RATE:
        return None
    located = sum(1 for i in grading.issues if i.start is not None)
    return round(located / len(grading.issues), 4)


def _recall(case: GoldenCase, grading: GradingResult) -> float | None:
    """期望指出的错处，被任意一条意见的原文片段覆盖住就算命中"""
    if not case.expect_fixes:
        return None
    originals = [i.original for i in grading.issues if i.original]
    hits = sum(1 for bad, _ in case.expect_fixes if any(bad in o or o in bad for o in originals))
    return round(hits / len(case.expect_fixes), 4)


def _fix_applied(case: GoldenCase, grading: GradingResult) -> float | None:
    """
    修订稿到底改没改：正确写法出现、错误写法消失，两者同时成立才算落地

    只查"正确写法在不在"会误判——原文里可能本来就有同样的词；
    只查"错误写法没了"也会误判——模型可能把整段删了。
    """
    if not case.expect_fixes:
        return None
    revised = grading.revised_text or ""
    hits = sum(1 for bad, good in case.expect_fixes if good in revised and bad not in revised)
    return round(hits / len(case.expect_fixes), 4)


def _type_coverage(case: GoldenCase, grading: GradingResult) -> float | None:
    """
    每组可接受类型只要出现一个就算覆盖，组与组之间才要求都有人管

    写死单值等于替模型裁决"的地得算错别字还是用词不当"——那是语言学争议而不是批改
    质量，指标会变成每换一家厂商红一次、还得重新解释为什么红。真正判对错的是
    recall 和 fix_rate，类型只是给用户看的标签。
    """
    if not case.expect_type_groups:
        return None
    found = {i.type for i in grading.issues}
    hits = sum(1 for group in case.expect_type_groups if found & set(group))
    return round(hits / len(case.expect_type_groups), 4)


def score_case(case: GoldenCase, grading: GradingResult) -> dict[str, Any]:
    """一份夹具的分数。字段全平铺，方便 run JSON 直接 diff"""
    n = len(grading.issues)
    over = n - case.max_issues if case.max_issues is not None else 0
    return {
        "id": case.id,
        "name": case.name,
        "format_ok": not grading.degraded,
        "issues": n,
        "min_issues_ok": n >= case.min_issues,
        "false_positives": max(0, over),
        "locate_rate": _located_ratio(grading),
        "recall": _recall(case, grading),
        "fix_rate": _fix_applied(case, grading),
        "type_coverage": _type_coverage(case, grading),
    }


# 参与聚合的指标：None 表示该用例没有这项期望，不进分母
RATIO_METRICS = ("locate_rate", "recall", "fix_rate", "type_coverage")


def aggregate(per_case: list[dict[str, Any]]) -> dict[str, Any]:
    """各指标取均值 + 布尔项按用例数折算，产出一轮的可比总数"""
    out: dict[str, Any] = {
        "cases": len(per_case),
        "issues_total": sum(c["issues"] for c in per_case),
    }

    for metric in RATIO_METRICS:
        values = [c[metric] for c in per_case if c[metric] is not None]
        out[metric] = round(sum(values) / len(values), 4) if values else None

    for flag in ("format_ok", "min_issues_ok"):
        out[f"{flag}_count"] = sum(1 for c in per_case if c[flag])

    out["false_positives"] = sum(c["false_positives"] for c in per_case)
    return out


def diff_runs(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """
    两轮对比，只报有变化的指标

    提示词改完最怕"感觉变好了"，这里给的是带符号的数字；阈值留给使用者判断，
    评测台不替人做决定。
    """
    lines: list[str] = []
    base, cur = baseline.get("aggregate", {}), current.get("aggregate", {})

    for key in RATIO_METRICS:
        b, c = base.get(key), cur.get(key)
        if b is None or c is None:
            continue
        delta = round(c - b, 4)
        if delta == 0:
            continue
        lines.append(f"{key:<14} {b:>7.4f} → {c:>7.4f}  {delta:+.4f}")

    for key in ("format_ok_count", "min_issues_ok_count", "issues_total", "false_positives"):
        b, c = base.get(key), cur.get(key)
        if isinstance(b, int) and isinstance(c, int) and b != c:
            lines.append(f"{key:<14} {b:>7} → {c:>7}  {c - b:+}")

    return lines
