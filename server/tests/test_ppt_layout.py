"""VLM 版面检查循环单测（feature-2 片3）——假 chat_json + 注入假渲染，不出网。

覆盖：pass 早停、A 页被点名→改走流式重排、流式页被点名→缩字号、
末轮仍 needs_revision 不再白渲染、解析失败降级不抛、渲染失败降级、
轮次硬上限、抽样首尾必送、修订映射越界安全。
"""

from __future__ import annotations

import asyncio
import io

import pymupdf
from pptx import Presentation

from services import ppt_layout
from services.llm import LLMError, LLMResult
from services.ppt_convert import render_blocks_with_meta
from services.ppt_layout import HARD_MAX_ROUNDS, _sample_indices, run_layout_loop

_PNG = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4)).tobytes("png")


def _result() -> LLMResult:
    return LLMResult(
        content="{}",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        latency_ms=50,
        model_name="mock-vlm",
        finish_reason="stop",
        attempts=1,
    )


class _Provider:
    id = 9
    name = "mock-vision"
    model_name = "mock-vlm"


def _para(text, bbox=(100, 100, 900, 200)):
    return {
        "type": "paragraph",
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
        "bbox": list(bbox),
    }


def _deck(pages, sizes):
    return render_blocks_with_meta(pages, {}, sizes)


def _fake_render(n_slides):
    def render(data, slide_count):
        assert slide_count == n_slides
        return [_PNG] * slide_count

    return render


def _texts(data: bytes) -> list[str]:
    prs = Presentation(io.BytesIO(data))
    return [s.text_frame.text for slide in prs.slides for s in slide.shapes if s.has_text_frame]


# 一页横向（A 模）+ 一页纵向（B 模）：slide0=横向 A，slide1=纵向 flow
_MIXED_PAGES = [
    [_para("横向页内容", bbox=(100, 400, 900, 500))],
    [_para("纵向页内容", bbox=(100, 100, 900, 200))],
]
_MIXED_SIZES = [(842, 474), (595, 842)]


def test_pass_early_stop(monkeypatch):
    calls = []

    async def fake_chat(messages, provider, *, json_schema=None):
        calls.append(messages)
        return _result(), {"verdict": "pass", "issues": []}

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    out, _out_meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=2,
            render=_fake_render(2),
        )
    )
    assert out == data  # pass 在第一轮 → 原样交付
    assert len(layout_calls) == 1 and layout_calls[0].verdict == "pass"
    assert len(calls) == 1


def test_page_slide_flagged_switches_to_flow(monkeypatch):
    payloads = [
        {"verdict": "needs_revision", "issues": [{"slide_index": 0, "category": "stacked"}]},
        {"verdict": "pass", "issues": []},
    ]

    async def fake_chat(messages, provider, *, json_schema=None):
        return _result(), payloads.pop(0)

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    assert meta[0]["mode"] == "page"

    def render(d, n):
        return [_PNG] * n

    out, out_meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=2,
            render=render,
        )
    )
    assert all(m["mode"] == "flow" for m in out_meta) and len(out_meta) >= 1
    assert "横向页内容" in _texts(out) and "纵向页内容" in _texts(out)
    assert [c.verdict for c in layout_calls] == ["needs_revision", "pass"]


def test_flow_slide_flagged_shrinks_font(monkeypatch):
    payloads = [
        {"verdict": "needs_revision", "issues": [{"slide_index": 1, "category": "overflow"}]},
        {"verdict": "pass", "issues": []},
    ]

    async def fake_chat(messages, provider, *, json_schema=None):
        return _result(), payloads.pop(0)

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    assert meta[1]["mode"] == "flow"
    out, _out_meta, _layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=2,
            render=_fake_render(2),
        )
    )
    prs = Presentation(io.BytesIO(out))
    body_sizes = [
        r.font.size.pt
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
        for para in shape.text_frame.paragraphs
        for r in para.runs
        if "纵向页内容" in r.text
    ]
    assert body_sizes and max(body_sizes) < 18  # 流式正文 18pt 被缩过
    assert "横向页内容" in _texts(out)  # 未被点名的 A 页字号也随全局缩放，但文字一字未动
    assert "纵向页内容" in _texts(out)


def test_last_round_needs_revision_no_wasted_render(monkeypatch):
    renders = {"n": 0}

    async def fake_chat(messages, provider, *, json_schema=None):
        return _result(), {
            "verdict": "needs_revision",
            "issues": [{"slide_index": 0, "category": "overlap"}],
        }

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)

    def render(d, n):
        renders["n"] += 1
        return [_PNG] * n

    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=2,
            render=render,
        )
    )
    assert len(layout_calls) == 2
    # 批评 2 轮，但重渲染只发生在第 1 轮之后：第 2 轮渲染的是修订版
    assert renders["n"] == 2
    assert out != data  # 交付的是修订版


def test_unparsable_critique_degrades_never_pass(monkeypatch):
    async def fake_chat(messages, provider, *, json_schema=None):
        return _result(), None  # 端点吐自由文本

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=3,
            render=_fake_render(2),
        )
    )
    assert out == data
    assert layout_calls[0].degraded is True
    assert layout_calls[0].verdict is None
    assert len(layout_calls) == 1  # "没听清"不当前提通过，也不空转下一轮


def test_llm_error_breaks_and_reports(monkeypatch):
    async def boom(messages, provider, *, json_schema=None):
        raise LLMError("LLM 请求超时", attempts=4)

    monkeypatch.setattr(ppt_layout, "chat_json", boom)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            render=_fake_render(2),
        )
    )
    assert out == data
    assert layout_calls[0].error == "LLM 请求超时" and layout_calls[0].attempts == 4


def test_render_failure_degrades_without_raising(monkeypatch):
    async def never(messages, provider, *, json_schema=None):  # pragma: no cover
        raise AssertionError("渲染挂了不该再外呼")

    monkeypatch.setattr(ppt_layout, "chat_json", never)

    def broken_render(data, n):
        raise RuntimeError("WSL 断了")

    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            render=broken_render,
        )
    )
    assert out == data
    assert layout_calls[0].degraded is True and "渲染失败" in layout_calls[0].error


def test_max_rounds_clamped_to_hard_cap(monkeypatch):
    seen = []

    async def fake_chat(messages, provider, *, json_schema=None):
        seen.append(1)
        return _result(), {
            "verdict": "needs_revision",
            "issues": [{"slide_index": 0, "category": "overlap"}],
        }

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)

    def render(d, n):
        return [_PNG] * n

    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=99,
            render=render,
        )
    )
    # 第 1 轮换分流，之后逐轮缩字号到下限（0.85→0.7225→0.7），第 5 轮到硬上限收手
    assert len(seen) == HARD_MAX_ROUNDS
    assert len(layout_calls) == HARD_MAX_ROUNDS
    assert out != data


def test_zero_rounds_disabled():
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=0,
            render=_fake_render(2),
        )
    )
    assert out == data and layout_calls == []


def test_out_of_range_and_dirty_issues_are_safe(monkeypatch):
    async def fake_chat(messages, provider, *, json_schema=None):
        return _result(), {
            "verdict": "needs_revision",
            "issues": [
                {"slide_index": 99, "category": "overlap"},
                "不是字典",
                {"slide_index": "x"},
            ],
        }

    monkeypatch.setattr(ppt_layout, "chat_json", fake_chat)
    data, meta = _deck(_MIXED_PAGES, _MIXED_SIZES)
    out, _meta, layout_calls = asyncio.run(
        run_layout_loop(
            _MIXED_PAGES,
            {},
            _MIXED_SIZES,
            _Provider(),
            1,
            first_data=data,
            first_meta=meta,
            max_rounds=3,
            render=_fake_render(2),
        )
    )
    assert out == data  # 全是越界/脏项 → 无改动 → 收手交付
    assert len(layout_calls) == 1


def test_sampling_includes_first_and_last():
    assert _sample_indices(3) == [0, 1, 2]
    idx = _sample_indices(100)
    assert len(idx) == 12
    assert idx[0] == 0 and idx[-1] == 99
    assert idx == sorted(set(idx))
