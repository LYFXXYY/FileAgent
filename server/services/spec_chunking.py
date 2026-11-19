"""规范文档的切块：一块 = 一条条款

为什么不复用批改那套 `split_document`（2026-11-19 实测后改的）：
规范文档是"编号条款 + 四段式标签"的结构化文本，按字符预算贪心装箱会把条款拦腰截断——
实测 66 条款 / 10,889 字在 2000 字预算下切成 6 块，**5 个边界全部腰斩**，
而断点正好落在【判定标准】之前。检索命中一块时拿到的是半条条款，
缺的恰好是最该注入 prompt 的判据部分。

所以这里改成按条款边界切：每块就是一条完整条款（含它所属的章标题），
`max_chars` 只是"单条条款的上限"，不是装箱目标。

三条不变式与 `services/chunking.py` 完全一致，且必须守住：
1. `chunk.text == source[chunk.start:chunk.end]`（块文本就是原文连续切片）
2. 块首尾相接、互不重叠
3. `reassemble(source, chunks, [c.text for c in chunks]) == source`

守不住第 1/3 条，"指回原文条款"就做不出来；守不住第 2 条，同一条款会被检索命中两次。
"""

from __future__ import annotations

import re
from itertools import pairwise

from services.chunking import Chunk, ChunkError, reassemble, split_document

# 条款号：形如 1.1.1 / 2.1 / 3.1.2.4，且后面必须紧跟非空白内容（排除正文里的纯数字行）
CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s+\S")
# 章标题：形如「第1章 错别字类」。章本身没有判据价值，必须并进它下面第一条条款的块里
CHAPTER_RE = re.compile(r"^\s*第\s*\d+\s*章\s+\S")


def _line_starts(source: str) -> list[tuple[int, str]]:
    """(行首绝对偏移, 行内容)，与 `_paragraph_spans` 同口径但保留空行位置。"""
    out: list[tuple[int, str]] = []
    cursor = 0
    for line in source.split("\n"):
        out.append((cursor, line))
        cursor += len(line) + 1
    return out


def _unit_starts(source: str) -> list[int]:
    """
    所有"结构边界"的偏移：条款号行 + 章标题行。

    返回空列表说明这份文档没有条款结构（用户传了篇散文当规范），调用方据此退化。
    """
    starts: list[int] = []
    for start, line in _line_starts(source):
        if CLAUSE_RE.match(line) or CHAPTER_RE.match(line):
            starts.append(start)
    return starts


def split_by_clause(source: str, max_chars: int = 2000) -> list[Chunk]:
    """
    按条款边界切块。每块是一条完整条款（章标题并入它下面第一条条款），
    超过 `max_chars` 的超长条款退回 `split_document` 按句细分，保证不吞字也不越界。

    没有条款结构时整体退回 `split_document`——规范库不挑文档，只是退化后检索粒度会变差。
    返回 [] 表示没有可索引内容。
    """
    if max_chars <= 0:
        raise ChunkError(f"单条预算必须是正整数，当前 {max_chars}")
    if not source or not source.strip():
        return []

    starts = _unit_starts(source)
    if not starts:
        return split_document(source, max_chars=max_chars)

    # 先给每个结构边界定性：章标题还是条款
    kinds: list[tuple[str, int, int]] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(source)
        first_line = source[s:e].split("\n")[0]
        kinds.append(("chapter" if CHAPTER_RE.match(first_line) else "clause", s, e))

    # 章标题自己没有任何判据，单独成块只会污染检索，一律与它下面第一条条款合成一块。
    # ⚠ 不能反过来"看上一块首行是不是章标题"再合并——合并后首行仍是章标题，
    # 那样会把后面的条款一路吞进来（第一版就是这么写错的）。
    units: list[tuple[int, int]] = []
    pending: tuple[int, int] | None = None
    for kind, s, e in kinds:
        if kind == "chapter":
            if pending is not None:
                units.append(pending)  # 连续两个章标题（少见），前一个先各自成块
            pending = (s, e)
            continue
        units.append((pending[0], e) if pending else (s, e))
        pending = None
    if pending is not None:
        units.append(pending)  # 文末孤立章标题，原样保留

    # 第一个结构边界之前的引导文字（文档标题等）挂到第一块上，别丢掉
    has_preamble = starts[0] > 0 and bool(source[: starts[0]].strip())

    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(units):
        if i == 0 and has_preamble:
            start = 0
        piece = Chunk(index=len(chunks), start=start, end=end, text=source[start:end])
        if piece.length <= max_chars:
            chunks.append(piece)
            continue
        # 超长条款：在本条款区间内按句细分，偏移量整体平移
        for sub in split_document(source[start:end], max_chars=max_chars):
            chunks.append(
                Chunk(
                    index=len(chunks),
                    start=start + sub.start,
                    end=start + sub.end,
                    text=source[start + sub.start : start + sub.end],
                )
            )

    _assert_invariants(source, chunks)
    return chunks


def _assert_invariants(source: str, chunks: list[Chunk]) -> None:
    """
    切完自查三条不变式。

    这里宁可抛错也不要静默产出坏块：坏块一旦入库，检索会给出看似合理实则错位的结果，
    那种问题事后几乎查不出来（同 §3.10 的判断）。
    """
    for c in chunks:
        if source[c.start : c.end] != c.text:
            raise ChunkError(f"块 {c.index} 的文本不是原文的连续切片")
    for a, b in pairwise(chunks):
        if b.start < a.end:
            raise ChunkError(f"块 {a.index} 与 {b.index} 区间重叠")
    if reassemble(source, chunks, [c.text for c in chunks]) != source:
        raise ChunkError("块拼回去不等于原文，偏移或区间有缺失")
