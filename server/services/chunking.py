"""
长文档分块 —— 把整篇切成连续、不重叠、能逐字拼回去的块

【为什么块必须能拼回原文】
分块批改的产物要重新变回**一篇**文档：问题清单的 `start/end` 是全局偏移（前端按整篇
原文高亮），修订文要按原位置拼回并保留段落分隔。只要块切在段落中间，这两件事都会
出问题，而错位没有任何信号——跟让模型输出坐标是同一类错误。

所以三条不变式，改这个文件必须全跑 `tests/test_chunking.py`：

1. **块文本就是原文的连续切片**：`chunk.text == source[chunk.start:chunk.end]`
2. **块首尾相接**：第一块从 0 开始，前一块的 `end` 不大于后一块的 `start`
3. **逐字拼回**：把每块内容原样放回，`reassemble()` 必须一字不差还原原文（含分隔符）

切点优先落在段落边界（`\n`），段落本身超预算时退到句末标点，还超就硬切。
后两种情况会把同一段拆开——这是有意的取舍：**突破上下文上限优先**，代价是跨切点的
片段可能定位不到（显示"未定位"而不是指错位置），比硬撑单发把整篇塞进去更安全。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 段落分隔：提取层的约定就是"一行一段"，见 AGENTS.md §3.8
_PARAGRAPH_SEP = "\n"

# 句末收束点（含可能被跟在后面的右引号/括号），长段落内部退这一档
_SENTENCE_END_RE = re.compile(r"[。．！？；.!?;][”』」\"'）)】]*")


class ChunkError(Exception):
    """分块参数不合理时抛出，让调用方在花钱之前就把配置错误暴露出来"""


@dataclass(frozen=True)
class Chunk:
    """原文里的一段连续区间，`[start, end)` 是相对整篇的绝对偏移"""

    index: int
    start: int
    end: int
    text: str

    @property
    def length(self) -> int:
        return self.end - self.start


def _paragraph_spans(source: str) -> list[tuple[int, int]]:
    """非空段落的 (start, end)，end 不含该段落末尾的换行"""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for line in source.split(_PARAGRAPH_SEP):
        if line.strip():
            spans.append((cursor, cursor + len(line)))
        cursor += len(line) + len(_PARAGRAPH_SEP)
    return spans


def _cut_points(unit_start: int, unit_text: str, max_chars: int) -> list[int]:
    """
    超长段落内部的切点：每次在预算内**尽量靠后**取句末标点，没有句点才按预算硬切

    取"预算内最后一个句点"而不是"第一个超预算的句点"，否则切出来的块本身还会超预算
    （第一版就是这么错的：25 字处有句点但预算 30，等到 50 才切，块直接 50 字）。
    """
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(unit_text)]
    total = len(unit_text)
    cuts: list[int] = []
    last = 0
    while total - last > max_chars:
        limit = last + max_chars
        options = [p for p in ends if last < p <= limit]
        nxt = max(options) if options else limit
        cuts.append(unit_start + nxt)
        last = nxt
    return cuts


def split_document(source: str, max_chars: int = 6000) -> list[Chunk]:
    """
    按预算把整篇切成连续块，贪心装到装不下为止；优先段落边界，其次句末标点。

    返回 [] 表示没有可批内容（空文档或全是空白），调用方据此走单发路径而不是报错。
    """
    if max_chars <= 0:
        raise ChunkError(f"分块预算必须是正整数，当前 {max_chars}")

    spans = _paragraph_spans(source)
    if not spans:
        return []

    chunks: list[Chunk] = []
    run_start = spans[0][0]
    run_end = spans[0][1]
    for start, end in spans[1:]:
        if end - run_start <= max_chars:
            run_end = end
            continue
        chunks.append(_make_chunk(len(chunks), source, run_start, run_end))
        run_start, run_end = start, end

    chunks.append(_make_chunk(len(chunks), source, run_start, run_end))

    # 单段本身就超预算的，二次细分。正常中文文档很少走到这里
    pieces = [c for chunk in chunks for c in _split_oversized(chunk, source, max_chars)]
    # 细分会改变块数，index 按最终顺序重编，别让调用方拿到重复下标
    return [_make_chunk(i, source, p.start, p.end) for i, p in enumerate(pieces)]


def _make_chunk(index: int, source: str, start: int, end: int) -> Chunk:
    return Chunk(index=index, start=start, end=end, text=source[start:end])


def _split_oversized(chunk: Chunk, source: str, max_chars: int) -> list[Chunk]:
    """把超过预算的块按句点/硬切拆开；没超预算的直接原样返回"""
    if chunk.length <= max_chars:
        return [chunk]

    cuts = _cut_points(chunk.start, chunk.text, max_chars)
    if not cuts:
        return [chunk]

    pieces: list[Chunk] = []
    left = chunk.start
    for right in [*cuts, chunk.end]:
        if right > left:
            pieces.append(_make_chunk(len(pieces), source, left, right))
            left = right
    return pieces


def reassemble(source: str, chunks: list[Chunk], parts: list[str]) -> str:
    """
    把逐块修订文按原位置拼回整篇：块区间换成模型给的文本，块之间的分隔符保持原样

    长度不齐就报错而不是悄悄补齐——少一块意味着整段内容凭空消失，那种 bug 必须响亮。
    """
    if len(parts) != len(chunks):
        raise ChunkError(f"块数与修订文段数不一致：{len(chunks)} 块 vs {len(parts)} 段")

    out: list[str] = []
    cursor = 0
    for chunk, part in zip(chunks, parts, strict=True):
        out.append(source[cursor : chunk.start])
        out.append(part)
        cursor = chunk.end
    out.append(source[cursor:])
    return "".join(out)
