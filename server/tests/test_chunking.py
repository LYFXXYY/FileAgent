"""
分块层单测 —— 三条不变式 + 退格路径

全纯函数，不碰 DB、不出网。改 chunking.py 必须全绿：
块拼不回原文的话，分块批改的清单偏移和修订文都是错的，而且错得没有信号。
"""

from itertools import pairwise

import pytest

from services.chunking import ChunkError, reassemble, split_document

DOC = "\n".join(
    [f"这是第{i}段的内容，用来占位以便触发分块逻辑，句长控制在十几个字左右。" for i in range(1, 9)]
)


def _assert_invariants(source: str, chunks, max_chars: int) -> None:
    """任何一次切分都必须同时满足这三条"""
    assert chunks, "非空文档不该切出 0 块"
    assert chunks[0].start == 0
    assert chunks[-1].end == len(source.rstrip()), "最后一块要吃掉最后一个非空段落"
    assert [c.index for c in chunks] == list(range(len(chunks))), "index 必须连续不重复"

    for prev, cur in pairwise(chunks):
        assert prev.end <= cur.start, "块不能重叠"
        # 块之间只允许隔着空白（段落分隔与空行），有别的内容就是漏批了一段
        assert not source[prev.end : cur.start].strip(), "块间夹了正文内容"

    for c in chunks:
        assert c.text == source[c.start : c.end]
        assert c.length <= max_chars, f"第 {c.index} 块超预算：{c.length} > {max_chars}"

    # 原样放回必须逐字等于原文，这是修订文拼接正确性的地基
    assert reassemble(source, chunks, [c.text for c in chunks]) == source


def test_empty_document_yields_no_chunks():
    assert split_document("", 100) == []
    assert split_document("\n\n   \n", 100) == []


def test_short_document_stays_single_chunk():
    chunks = split_document("一句话。\n两句话。", max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0].text == "一句话。\n两句话。"


def test_paragraphs_pack_until_budget():
    chunks = split_document(DOC, max_chars=120)
    _assert_invariants(DOC, chunks, 120)
    assert len(chunks) > 1


def test_cuts_land_on_paragraph_boundaries():
    """正常文档不该把句子切断：每块末尾要么是 \n，要么是全文结束"""
    chunks = split_document(DOC, max_chars=120)
    for c in chunks[:-1]:
        assert DOC[c.end] == "\n", f"第 {c.index} 块切在段落中间"


def test_blank_lines_are_preserved_as_gaps():
    source = "第一段。\n\n\n第二段。\n\n第三段。"
    chunks = split_document(source, max_chars=6)
    _assert_invariants(source, chunks, 6)
    assert reassemble(source, chunks, ["A", "B", "C"]) == "A\n\n\nB\n\nC"


def test_oversized_paragraph_falls_back_to_sentence_ends():
    sentences = "。".join(f"第{i}个子句内容差不多长" for i in range(40)) + "。"
    assert "\n" not in sentences
    chunks = split_document(sentences, max_chars=80)
    _assert_invariants(sentences, chunks, 80)
    for c in chunks[:-1]:
        assert sentences[c.end - 1] == "。", f"第 {c.index} 块没收到句末"


def test_runaway_text_without_punctuation_hard_cuts():
    """整段没有任何标点时只能硬切，但仍不许超预算、仍要拼得回去"""
    source = "无" * 500
    chunks = split_document(source, max_chars=120)
    _assert_invariants(source, chunks, 120)
    assert len(chunks) == 5


def test_mixed_document_with_one_huge_paragraph():
    source = f"正常段落。\n{'句子。' * 200}\n结尾段落。"
    chunks = split_document(source, max_chars=90)
    _assert_invariants(source, chunks, 90)


def test_budget_must_be_positive():
    """配置写错要在花钱之前就炸，而不是切出一堆碎块"""
    with pytest.raises(ChunkError):
        split_document(DOC, max_chars=0)
    with pytest.raises(ChunkError):
        split_document(DOC, max_chars=-5)


def test_reassemble_rejects_missing_parts():
    """少一段意味着整段内容凭空消失，必须响亮报错不能静默补齐"""
    chunks = split_document(DOC, max_chars=120)
    with pytest.raises(ChunkError):
        reassemble(DOC, chunks, ["只有一段"])


def test_reassemble_replaces_only_chunk_spans():
    chunks = split_document("甲段。\n乙段。\n丙段。", max_chars=4)
    parts = ["【甲】", "【乙】", "【丙】"]
    assert reassemble("甲段。\n乙段。\n丙段。", chunks, parts) == "【甲】\n【乙】\n【丙】"
