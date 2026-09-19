"""
Diff 服务 —— 逐字对比原文 vs LLM 修订文，标出改动

【核心算法】
- difflib.SequenceMatcher：Python 标准库，找出两个序列的"最长公共子序列"
- 类比 Java 的 diff-match-patch 库

【输出格式】
返回 list[(op, text)]：
  op = "equal"   → 未改动的原文（黑色）
  op = "insert"  → LLM 新增/修改的部分（红色）
  op = "delete"  → 被 LLM 删除的部分（红色 + 删除线）
"""

from __future__ import annotations

import difflib
from typing import Literal

DiffOp = Literal["equal", "insert", "delete"]


def compute_diff(original: str, revised: str) -> list[tuple[DiffOp, str]]:
    """
    逐字符 diff。返回按顺序排列的 (操作类型, 文本片段) 列表。

    autojunk=False 关闭 "常见元素视为噪声" 优化——对中文文本很重要，
    否则 SequenceMatcher 会把高频字（的、是、在）当噪声处理导致结果不准。
    """
    matcher = difflib.SequenceMatcher(a=original, b=revised, autojunk=False)
    result: list[tuple[DiffOp, str]] = []

    # get_opcodes() 返回如 [('equal', 0, 10, 0, 10), ('replace', 10, 15, 10, 13), ...]
    # 参数意义：(op, a_start, a_end, b_start, b_end)
    for op, a_lo, a_hi, b_lo, b_hi in matcher.get_opcodes():
        if op == "equal":
            result.append(("equal", original[a_lo:a_hi]))
        elif op == "replace":
            # "replace" 拆成 delete + insert，方便 Word 层分别处理样式
            old_text = original[a_lo:a_hi]
            new_text = revised[b_lo:b_hi]
            if old_text:
                result.append(("delete", old_text))
            if new_text:
                result.append(("insert", new_text))
        elif op == "delete":
            result.append(("delete", original[a_lo:a_hi]))
        elif op == "insert":
            result.append(("insert", revised[b_lo:b_hi]))

    return result
