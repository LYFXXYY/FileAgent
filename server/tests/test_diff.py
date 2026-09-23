"""compute_diff 单测 —— 逐字符 diff 的三种操作类型"""

from services.diff import compute_diff


def test_equal_when_identical():
    result = compute_diff("你好世界", "你好世界")
    assert result == [("equal", "你好世界")]


def test_insert_when_revised_adds_text():
    result = compute_diff("你好", "你好世界")
    assert ("insert", "世界") in result
    # 原文部分保持 equal
    assert ("equal", "你好") in result


def test_delete_when_revised_removes_text():
    result = compute_diff("你好世界", "你好")
    assert ("delete", "世界") in result
    assert ("equal", "你好") in result


def test_replace_splits_into_delete_then_insert():
    # "替换" → "改写"：应拆成 delete(替换) + insert(改写)，且 delete 在前
    result = compute_diff("替换", "改写")
    ops = [op for op, _ in result]
    assert "delete" in ops and "insert" in ops
    assert ops.index("delete") < ops.index("insert")


def test_chinese_high_freq_chars_not_treated_as_junk():
    # autojunk=False 的关键：高频字（的/是/在）不应被当噪声
    original = "这是我的书"
    revised = "这是你的书"
    result = compute_diff(original, revised)
    # 我→你 应体现为 delete+insert，其余保持 equal
    assert ("delete", "我") in result
    assert ("insert", "你") in result
    assert ("equal", "这是") in result


def test_empty_inputs():
    assert compute_diff("", "") == []
    assert compute_diff("", "abc") == [("insert", "abc")]
    assert compute_diff("abc", "") == [("delete", "abc")]
