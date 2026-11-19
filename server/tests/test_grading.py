"""
结构化批改单测 —— 原文定位、类型归一化、解析降级、序列化往返

纯函数 + 内存，不连 Postgres、不出网。
"""

import pytest

from schemas.grading import ISSUE_TYPES, GradingIssue, GradingResult, normalize_type
from services.grading import (
    GRADING_OUTPUT_INSTRUCTION,
    build_messages,
    deserialize_issues,
    deserialize_refs,
    is_degraded,
    locate_issues,
    parse_grading_output,
    serialize_result,
    spec_note,
)

# 一段含重复片段的原文，用于验证定位
ORIGIN = "第一段内容。\n他高兴的跳了起来。\n重复用词。重复用词。重复用词。"


def _issue(fragment: str, **kwargs) -> GradingIssue:
    return GradingIssue(original=fragment, **kwargs)


# ---------- 类型归一化：开放输入 + 后端归一，绝不报错 ----------
def test_normalize_type_accepts_chinese_alias():
    assert normalize_type("错别字") == "typo"
    assert normalize_type("前后不一致") == "logic"


def test_normalize_type_is_case_and_delimiter_insensitive():
    assert normalize_type("  Grammar  ") == "grammar"
    assert normalize_type("  TYPO  ") == "typo"


def test_normalize_type_unknown_becomes_other():
    """类型名由模型自由发挥，认不出来归 other，不能因此丢掉整条意见"""
    assert normalize_type("排版建议") == "other"


def test_normalize_type_non_string_becomes_other():
    assert normalize_type(None) == "other"
    assert normalize_type(123) == "other"


def test_all_canonical_types_present():
    assert ISSUE_TYPES == (
        "typo",
        "grammar",
        "wording",
        "punctuation",
        "structure",
        "logic",
        "content",
        "other",
    )


def test_issue_validates_empty_fragment():
    with pytest.raises(ValueError):
        GradingIssue(original="")


def test_issue_clamps_confidence():
    with pytest.raises(ValueError):
        GradingIssue(original="x", confidence=1.5)


# ---------- 定位：坐标由后端算，不猜 ----------
def test_locate_finds_span_of_quoted_fragment():
    (issue,) = locate_issues([_issue("他高兴的跳了起来")], ORIGIN)

    assert issue.start is not None
    assert ORIGIN[issue.start : issue.end] == "他高兴的跳了起来"


def test_locate_leaves_none_when_fragment_absent():
    """匹配不到就留 None，绝不指一个错位置"""
    (issue,) = locate_issues([_issue("这段根本不在原文里")], ORIGIN)

    assert issue.start is None
    assert issue.end is None


def test_locate_assigns_successive_occurrences_to_distinct_issues():
    """同一个片段被提多次 → 依次认领不同出现位置，不能全指第一处"""
    located = locate_issues([_issue("重复用词"), _issue("重复用词")], ORIGIN)

    assert located[0].start != located[1].start
    assert located[0].start == ORIGIN.index("重复用词")


def test_locate_does_not_overwrite_input_list():
    """不可变性：返回新对象，入参列表不被就地修改"""
    original = [_issue("重复用词")]

    located = locate_issues(original, ORIGIN)

    assert located is not original
    assert original[0].start is None


def test_locate_spans_do_not_overlap():
    text = "甲乙丙。甲乙丙。甲乙丙。"
    located = locate_issues([_issue("甲乙丙"), _issue("甲乙丙"), _issue("甲乙丙")], text)

    spans = [(i.start, i.end) for i in located]
    assert len(set(spans)) == 3


def test_locate_empty_original_text():
    (issue,) = locate_issues([_issue("x")], "")

    assert issue.start is None


# ---------- 解析：不合格一律降级，不报错 ----------
def test_parse_happy_path():
    payload = {
        "revised_text": "第一段内容。\n他高兴地跳了起来。\n重复用词。",
        "issues": [
            {
                "type": "typo",
                "original": "他高兴的跳了起来",
                "suggestion": "他高兴地跳了起来",
                "reason": "的地不分",
                "confidence": 0.9,
            }
        ],
    }

    result = parse_grading_output("ignored", payload, ORIGIN)

    assert result.degraded is False
    assert len(result.issues) == 1
    assert result.issues[0].start is not None


def test_parse_missing_revised_text_degrades():
    """缺 revised_text 就没法做 diff 下载，整条降级而不是去猜一份"""
    result = parse_grading_output("模型的自由文本输出", {"issues": []}, ORIGIN)

    assert result.degraded is True
    assert result.revised_text == "模型的自由文本输出"
    assert result.issues == []


def test_parse_none_payload_degrades():
    """chat_json 返回 None = 端点压根没吐 JSON"""
    result = parse_grading_output("这不是 JSON", None, ORIGIN)

    assert result.degraded is True
    assert result.revised_text == "这不是 JSON"


def test_parse_non_list_issues_yields_empty():
    result = parse_grading_output("x", {"revised_text": "正文", "issues": "oops"}, ORIGIN)

    assert result.degraded is False
    assert result.issues == []


def test_parse_drops_invalid_issue_keeps_valid_ones():
    """单条不合格只丢那一条，不连累整份清单"""
    payload = {
        "revised_text": "正文",
        "issues": [
            {
                "type": "typo",
                "original": "第一段内容",
                "suggestion": "",
                "reason": "",
                "confidence": 0.8,
            },
            {
                "type": "typo",
                "original": "",
                "suggestion": "",
                "reason": "",
                "confidence": 0.8,
            },
        ],
    }

    result = parse_grading_output("x", payload, ORIGIN)

    assert len(result.issues) == 1


def test_parse_coerces_null_text_fields():
    payload = {
        "revised_text": "正文",
        "issues": [
            {
                "type": "typo",
                "original": "高兴",
                "suggestion": None,
                "reason": None,
                "confidence": 0.6,
            }
        ],
    }

    result = parse_grading_output("x", payload, ORIGIN)

    assert result.issues[0].suggestion == ""
    assert result.issues[0].reason == ""


# ---------- 序列化往返 ----------
def test_serialize_deserialize_roundtrip():
    issues = locate_issues([_issue("他高兴的跳了起来")], ORIGIN)
    stored = serialize_result(GradingResult(revised_text="修订", issues=issues))

    restored = deserialize_issues(stored)

    assert len(restored) == 1
    assert restored[0]["original"] == "他高兴的跳了起来"
    assert restored[0]["start"] is not None


def test_deserialize_handles_none_and_garbage():
    """只读展示路径坏数据不能抛 500，解析不了就当无清单"""
    assert deserialize_issues(None) == []
    assert deserialize_issues("{这不是合法 json") == []
    assert deserialize_issues("[1, 2]") == []


def test_is_degraded_handles_none_and_garbage():
    assert is_degraded(None) is False
    assert is_degraded("{bad") is False
    assert is_degraded("[1]") is False


def test_is_degraded_reads_flag():
    stored = serialize_result(GradingResult(revised_text="自由文本", issues=[], degraded=True))

    assert is_degraded(stored) is True
    assert deserialize_issues(stored) == []


def test_serialize_keeps_chinese_readable():
    stored = serialize_result(GradingResult(revised_text="正文", issues=[_issue("高兴")]))

    assert "\\u" not in stored  # ensure_ascii=False 生效


# ---------- 消息构造 ----------
def test_build_messages_system_then_user():
    system, user = build_messages("请批改下面的正文")

    assert system["role"] == "system"
    assert user["role"] == "user"
    assert user["content"] == "请批改下面的正文"


def test_contract_forbids_model_outputting_coordinates():
    """契约必须明说不要行号/偏移，否则模型会自己编坐标"""
    system, _ = build_messages("x")

    assert "偏移" in system["content"]
    assert "行号" not in system["content"] or "不要" in system["content"]


def test_contract_mentions_json_for_json_object_rung():
    """json_object 档要求提示词含 json 字样，否则端点直接 400"""
    system, _ = build_messages("x")

    assert "JSON" in system["content"]


def test_contract_requires_revised_text_field():
    system, _ = build_messages("x")

    assert "revised_text" in system["content"]
    assert "issues" in system["content"]


# ---------- 规范条款注入（阶段 6 A3） ----------
def test_build_messages_without_spec_is_unchanged():
    """
    不传条款时 messages 与阶段 6 之前逐字一致。

    这条钉住"未启用 RAG 的批改链路不受影响"——用户明确要求先保证这一点，
    注入逻辑一旦改动了默认路径的任何一个字节，这里就该红。
    """
    msgs = build_messages("请批改：{{content}}")

    assert len(msgs) == 2
    assert msgs[1]["content"] == "请批改：{{content}}"


def test_spec_context_goes_to_user_message_not_system():
    """
    条款是内容不是契约，进 user。

    混进 system 会让模型把输出格式约束当材料一起改写（§3.7 立的边界），
    出格率上升而且是静默的。
    """
    msgs = build_messages("批改这份文档", spec_context="【1.1.1】同音别字……")

    assert msgs[0]["content"] == GRADING_OUTPUT_INSTRUCTION  # system 契约原样
    assert "1.1.1" in msgs[1]["content"]
    assert "JSON" not in msgs[1]["content"]  # 契约没被搬进 user


def test_spec_context_is_appended_after_prompt():
    """条款排在用户要求与正文之后：模型对尾部更敏感，也保证原文 prompt 不被改写"""
    msgs = build_messages("原有要求", spec_context="条款甲")

    assert msgs[1]["content"].startswith("原有要求")
    assert "条款甲" in msgs[1]["content"]


def test_spec_context_tells_model_not_to_force_citations():
    """
    必须写明"不适用就不要硬套"。

    否则模型会给每条意见都挂一个不相干的条款号，引用看起来更权威、实际更假。
    """
    msgs = build_messages("要求", spec_context="条款甲")

    assert "不要引用" in msgs[1]["content"]


# ---------- 引用溯源落在信封里 ----------
def test_refs_roundtrip_through_envelope():
    refs = [{"clause": "1.1.1", "score": 0.51, "doc_id": 3, "seq": 0}]
    stored = serialize_result(GradingResult(revised_text="修订", issues=[]), refs=refs, note=None)

    assert deserialize_refs(stored) == refs
    assert spec_note(stored) is None
    # 老字段一个都不能少：issues 展示路径读的是同一份信封
    assert deserialize_issues(stored) == []


def test_note_roundtrip_and_absence():
    stored = serialize_result(GradingResult(revised_text="x", issues=[]), note="端点未就绪")

    assert spec_note(stored) == "端点未就绪"
    assert deserialize_refs(stored) == []


def test_envelope_without_refs_has_no_new_keys():
    """
    没用规范库时信封形状与阶段 6 之前完全一致。

    多写一个空 refs 会让"没用规范"和"用了但没引用"在数据上分不开，
    也平白改变历史数据的对比基线。
    """
    import json

    stored = serialize_result(GradingResult(revised_text="x", issues=[]))

    assert set(json.loads(stored)) == {"degraded", "issues"}


def test_refs_and_note_survive_garbage():
    assert deserialize_refs(None) == []
    assert deserialize_refs("[1]") == []
    assert spec_note(None) is None
    assert spec_note('{"spec_note": 123}') is None  # 类型不对当没有
