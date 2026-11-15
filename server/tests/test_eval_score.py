"""
评测台打分器单测 —— 纯函数，不出网、不碰 DB

这一组用例本身就是"评测台可信不可信"的门禁：指标算错，后面所有 A/B 结论都作废。
"""

from eval.cases import CASES, case_by_id
from eval.score import aggregate, diff_runs, score_case
from schemas.grading import GradingIssue, GradingResult


def _issue(original: str, *, issue_type: str = "typo", start: int | None = 0, suggestion: str = ""):
    return GradingIssue(original=original, type=issue_type, start=start, suggestion=suggestion)


# 固定一份"的地得"夹具当被测对象
CASE = case_by_id("de-di-de")


def test_score_case_counts_located_issues():
    grading = GradingResult(
        revised_text="他高兴地跳了起来",
        issues=[_issue("他高兴的跳了起来", start=0), _issue("考的太好了", start=None)],
    )
    assert score_case(CASE, grading)["locate_rate"] == 0.5


def test_recall_counts_issues_covering_expected_errors():
    grading = GradingResult(
        revised_text="",
        issues=[_issue("他高兴的跳了起来"), _issue("随便一句无关的")],
    )
    # 期望 6 处，命中 1 处（分数按 4 位小数落库，断言跟着取整）
    assert score_case(CASE, grading)["recall"] == 0.1667


def test_fix_rate_detects_revision_not_applied():
    """
    这条就是评测台存在的第一理由：意见全对但修订稿原样退回，必须打出 0 分

    真机第一轮（qwen3.7-flash）就是这个形态，只统计 degraded 和条数完全看不出来。
    """
    unchanged = GradingResult(
        revised_text=CASE.doc,
        issues=[_issue(bad) for bad, _ in CASE.expect_fixes],
    )
    assert score_case(CASE, unchanged)["fix_rate"] == 0.0

    fixed = GradingResult(
        revised_text=CASE.doc.replace("高兴的跳", "高兴地跳").replace("考的太好", "考得太好"),
        issues=[_issue("他高兴的跳了起来")],
    )
    assert score_case(CASE, fixed)["fix_rate"] == 0.3333


def test_fix_rate_is_none_when_case_has_no_string_fixes():
    """结构类夹具没有唯一正确写法，指标必须是 n/a 而不是 0 分，否则聚合被假 0 拉平"""
    clean = case_by_id("structure")
    graded = GradingResult(
        revised_text=clean.doc, issues=[_issue("第一段", issue_type="structure")]
    )
    assert score_case(clean, graded)["fix_rate"] is None
    assert score_case(clean, graded)["recall"] is None


def test_clean_case_counts_over_limit_issues_as_false_positives():
    clean = case_by_id("clean")
    graded = GradingResult(
        revised_text=clean.doc,
        issues=[_issue("星期天的早晨"), _issue("菜市场"), _issue("道谢离开")],
    )
    scored = score_case(clean, graded)
    assert scored["false_positives"] == 1  # 上限 2 条，挑出 3 条就是 1 条误报


def test_type_coverage_requires_each_type_present():
    clean = case_by_id("structure")
    one_only = GradingResult(
        revised_text="",
        issues=[_issue("读书很重要", issue_type="structure")],
    )
    assert score_case(clean, one_only)["type_coverage"] == 0.5


def test_score_case_flags_format_degradation():
    degraded = GradingResult(revised_text="自由文本", issues=[], degraded=True)
    scored = score_case(CASE, degraded)
    assert scored["format_ok"] is False
    assert scored["min_issues_ok"] is False


def test_aggregate_ignores_none_metrics():
    rows = [
        {
            "issues": 2,
            "locate_rate": 1.0,
            "recall": 0.5,
            "fix_rate": None,
            "type_coverage": None,
            "format_ok": True,
            "min_issues_ok": True,
            "false_positives": 0,
        },
        {
            "issues": 1,
            "locate_rate": 0.0,
            "recall": None,
            "fix_rate": None,
            "type_coverage": None,
            "format_ok": True,
            "min_issues_ok": False,
            "false_positives": 0,
        },
    ]
    agg = aggregate(rows)
    assert agg["locate_rate"] == 0.5
    assert agg["recall"] == 0.5  # 只有一个非 None，不参与平均的分母
    assert agg["fix_rate"] is None
    assert agg["format_ok_count"] == 2
    assert agg["min_issues_ok_count"] == 1


def test_diff_runs_reports_signed_delta_and_skips_unchanged():
    base = {"aggregate": {"locate_rate": 0.5, "recall": 1.0, "issues_total": 4}}
    cur = {"aggregate": {"locate_rate": 0.75, "recall": 1.0, "issues_total": 6}}
    lines = diff_runs(base, cur)
    assert any("locate_rate" in line and "+0.2500" in line for line in lines)
    assert not any("recall" in line for line in lines)
    assert any("issues_total" in line and "+2" in line for line in lines)


def test_every_case_is_self_consistent():
    """
    夹具的期望必须落在自己的文档里

    写夹具时最容易犯的错是把"错处"改顺了再粘回去，于是 recall 永远是 0，
    评测台静悄悄变成一台 measuring nothing 的机器。
    """
    for case in CASES:
        for bad, good in case.expect_fixes:
            assert bad in case.doc, f"{case.id}: 期望的错处 {bad!r} 不在文档里"
            assert bad != good, f"{case.id}: {bad!r} 的错处和正确写法相同"
        if case.max_issues is not None:
            assert case.max_issues >= case.min_issues, f"{case.id}: 上下限写反了"
