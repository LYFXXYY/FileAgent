"""规范切块器测试：一块一条款 + 三条不变式 + 真实夹具的判据完整性。

为什么单独测这个而不靠 test_spec_storage：那边验的是"入库链路"，
这边验的是"切出来的粒度对不对"，而粒度错了不会报错，只会让检索悄悄变蠢。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from itertools import pairwise

from services.chunking import ChunkError, reassemble
from services.spec_chunking import CLAUSE_RE, split_by_clause

SPEC = """文字编辑校对规范（教学示例稿）

第1章 错别字类

1.1.1 同音别字
【适用情形】文本中出现读音相同但含义不同的汉字。
【判定标准】替换为同音字后语义不通即判定。
【错误示例】他需要再接再励。
【正确示例】他需要再接再厉。

1.1.2 形近别字
【适用情形】字形相近的字写错。
【判定标准】核对部首笔画与词义。
【错误示例】提纲写得很差。
【正确示例】提纲写得很草。

第2章 语法类

2.1.1 语序不当
【适用情形】多个定语顺序不合习惯。
【判定标准】按领属、数量、动词短语的顺序排列。
【错误示例】他很学习努力。
【正确示例】他学习很努力。
"""

CLAUSE_IDS = ["1.1.1", "1.1.2", "2.1.1"]


def _invariants_ok(source, chunks):
    if any(source[c.start : c.end] != c.text for c in chunks):
        return False
    if any(b.start < a.end for a, b in pairwise(chunks)):
        return False
    return reassemble(source, chunks, [c.text for c in chunks]) == source


class TestClausePerChunk:
    def test_每块一条条款(self):
        chunks = split_by_clause(SPEC, max_chars=2000)
        assert len(chunks) == len(CLAUSE_IDS)

    def test_每块首行都是条款号或章标题(self):
        from services.spec_chunking import CHAPTER_RE

        for ch in split_by_clause(SPEC, max_chars=2000):
            first = next(line for line in ch.text.split("\n") if line.strip())
            ok = CLAUSE_RE.match(first) or CHAPTER_RE.match(first) or first.startswith("文字编辑")
            assert ok, first

    def test_章标题不单独成块而是并进首条条款(self):
        first = split_by_clause(SPEC, max_chars=2000)[0]
        assert "第1章 错别字类" in first.text and "1.1.1" in first.text

    def test_每条条款的判据都完整落在自己块内(self):
        for ch in split_by_clause(SPEC, max_chars=2000):
            ids = [m.group(1) for line in ch.text.split("\n") if (m := CLAUSE_RE.match(line))]
            if "1.1.1" in ids:
                assert "再接再励" in ch.text and "再接再厉" in ch.text
            if "2.1.1" in ids:
                assert "【判定标准】" in ch.text

    def test_引导文字挂在第一块不丢失(self):
        chunks = split_by_clause(SPEC, max_chars=2000)
        assert chunks[0].start == 0
        assert "文字编辑校对规范" in chunks[0].text


class TestInvariants:
    def test_三条不变式(self):
        chunks = split_by_clause(SPEC, max_chars=2000)
        assert _invariants_ok(SPEC, chunks)

    def test_超长条款退回句切后不变式仍成立(self):
        long_clause = "9.9.9 超长条款\n" + "这是一句用来撑长度的判据说明。" * 40
        source = "9.9.9 起\n" + long_clause
        chunks = split_by_clause(source, max_chars=120)
        assert len(chunks) > 1
        assert _invariants_ok(source, chunks)
        assert all(c.length <= 120 or i == 0 for i, c in enumerate(chunks))

    def test_预算内不跨条款合并(self):
        # 200 字预算足够装下每条条款，绝不能像贪心装箱那样把三条塞一块
        chunks = split_by_clause(SPEC, max_chars=200)
        for ch in chunks:
            ids = [m.group(1) for line in ch.text.split("\n") if (m := CLAUSE_RE.match(line))]
            assert len(ids) <= 1, ids


class TestDegradation:
    def test_没有条款结构时退回普通切块(self):
        from services.chunking import split_document

        prose = "第一段内容。\n第二段内容。\n第三段内容。"
        assert [c.text for c in split_by_clause(prose, max_chars=2000)] == [
            c.text for c in split_document(prose, max_chars=2000)
        ]

    def test_空文本返回空列表(self):
        assert split_by_clause("", max_chars=2000) == []
        assert split_by_clause("   \n\n  ", max_chars=2000) == []

    def test_非法预算抛错(self):
        with pytest.raises(ChunkError):
            split_by_clause(SPEC, max_chars=0)


class TestRealFixture:
    """真实规范文档：66 条款就该是 66 块，且一块不缺判据。"""

    FIX = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "specs", "文字编辑校对规范（教学示例稿）.docx"
    )

    def test_夹具存在则逐条完整(self):
        if not os.path.exists(self.FIX):
            pytest.skip("规范夹具未入库，跳过")
        from docx import Document

        text = "\n".join(p.text for p in Document(self.FIX).paragraphs if p.text.strip())
        chunks = split_by_clause(text, max_chars=2000)
        clause_total = sum(1 for line in text.split("\n") if CLAUSE_RE.match(line))

        assert len(chunks) == clause_total
        assert _invariants_ok(text, chunks)
        # 腰斩的直接反证：每块都得带着【判定标准】
        missing = [c.index for c in chunks if "【判定标准】" not in c.text]
        assert not missing, f"这些块缺判据：{missing}"
