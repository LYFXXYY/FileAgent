"""VLM 坏块修复单测（feature-2 片2）——假 chat_json，不出网不连库。

覆盖：坏块判据、逐块装配（不可变）、结构校验降级、失败不抛、成本上限、
区域裁剪真实可用（裁不出就不修）、渲染器"LLM 修复"标记。
"""

from __future__ import annotations

import asyncio
import io

import pymupdf
import pytest
from pptx import Presentation

from services import ppt_repair
from services.llm import LLMError, LLMResult
from services.ppt_convert import render_blocks_to_pptx
from services.ppt_repair import (
    MAX_REPAIR_BLOCKS,
    RepairCall,
    crop_region,
    find_defects,
    repair_defective_blocks,
)


def _result() -> LLMResult:
    return LLMResult(
        content="{}",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        latency_ms=100,
        model_name="mock-vlm",
        finish_reason="stop",
        attempts=1,
    )


def _para(text, bbox=(100, 100, 900, 200)):
    return {
        "type": "paragraph",
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
        "bbox": list(bbox) if bbox else None,
    }


def _title(text, level=2):
    return {
        "type": "title",
        "content": {"title_content": [{"type": "text", "content": text}], "level": level},
        "bbox": [100, 60, 900, 100],
    }


def _table(html=None):
    content = {"image_source": {"path": "images/t.jpg"}}
    if html is not None:
        content["html"] = html
    return {"type": "table", "content": content, "bbox": [100, 300, 900, 500]}


def _pdf(pages=2) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"第{i + 1}页真实文字", fontname="china-s", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


class _FakeProvider:
    id = 99
    name = "mock-vision"
    model_name = "mock-vlm"


def _repair(pages, pdf=None, sizes=None):
    return asyncio.run(
        repair_defective_blocks(
            pdf if pdf is not None else _pdf(), pages, sizes or [(595.0, 842.0)], _FakeProvider(), 7
        )
    )


# ---------- 坏块判据 ----------
def test_find_defects_detects_empty_text_and_broken_table():
    pages = [
        [
            _para(""),
            _para("正常段落"),
            _table(html="<div>不是表格</div>"),
            _table(html="<table><tr><td>a</td></tr></table>"),
            _title(""),
        ]
    ]
    defects = find_defects(pages)
    assert (0, 0, "text") in defects
    assert (0, 2, "table") in defects
    assert (0, 4, "text") in defects
    assert len(defects) == 3


def test_find_defects_skips_blocks_without_bbox():
    assert find_defects([[_para("", bbox=None)]]) == []


# ---------- 修复装配 ----------
def test_repair_text_replaces_content_immutably(monkeypatch):
    captured: dict = {}

    async def fake_chat(messages, provider):
        captured["messages"] = messages
        return _result(), {"content": "修复后的正文。"}

    monkeypatch.setattr(ppt_repair, "chat_json", fake_chat)
    pages = [[_para("")]]
    new_pages, calls = _repair(pages)
    assert calls[0].degraded is False and calls[0].result is not None
    block = new_pages[0][0]
    assert block["repaired"] is True
    assert block["content"]["paragraph_content"] == [{"type": "text", "content": "修复后的正文。"}]
    # 原 pages 不被就地修改（不可变约定）
    assert pages[0][0]["content"]["paragraph_content"][0]["content"] == ""
    assert "repaired" not in pages[0][0]
    # grounding 必须带原图 + 标记可被假端点识别
    blob = str(captured["messages"])
    assert "image_url" in blob and "data:image/png;base64" in blob
    assert "[ppt-repair:log-7]" in blob


def test_repair_title_keeps_level(monkeypatch):
    async def fake_chat(messages, provider):
        return _result(), {"content": "修复标题"}

    monkeypatch.setattr(ppt_repair, "chat_json", fake_chat)
    new_pages, _calls = _repair([[_title("")]])
    block = new_pages[0][0]
    assert block["content"]["level"] == 2
    assert block["content"]["title_content"] == [{"type": "text", "content": "修复标题"}]


def test_repair_table_accepts_valid_html(monkeypatch):
    html = "<table><tr><td rowspan=1 colspan=1>列A</td></tr></table>"

    async def fake_chat(messages, provider):
        return _result(), {"content": html}

    monkeypatch.setattr(ppt_repair, "chat_json", fake_chat)
    new_pages, calls = _repair([[_table()]])
    assert calls[0].degraded is False
    assert new_pages[0][0]["content"]["html"] == html
    assert new_pages[0][0]["repaired"] is True


@pytest.mark.parametrize(
    ("block", "payload"),
    [
        (_para(""), {"content": "   "}),
        (_table(), {"content": "没有表格标签"}),
        (_table(), {"content": ""}),
    ],
    ids=["空文字", "表格无标签", "表格空产出"],
)
def test_repair_degrades_and_keeps_block(monkeypatch, block, payload):
    async def fake_chat(messages, provider):
        return _result(), payload

    monkeypatch.setattr(ppt_repair, "chat_json", fake_chat)
    pages = [[block]]
    new_pages, calls = _repair(pages)
    assert calls[0].degraded is True
    assert "repaired" not in new_pages[0][0]


def test_repair_llm_error_keeps_block_and_reports_error(monkeypatch):
    async def boom(messages, provider):
        raise LLMError("LLM 请求超时", attempts=3)

    monkeypatch.setattr(ppt_repair, "chat_json", boom)
    pages = [[_para("")]]
    new_pages, calls = _repair(pages)
    assert calls == [RepairCall(result=None, error="LLM 请求超时", attempts=3)]
    assert "repaired" not in new_pages[0][0]


def test_repair_respects_cost_cap(monkeypatch):
    count = {"n": 0}

    async def fake_chat(messages, provider):
        count["n"] += 1
        return _result(), {"content": "x"}

    monkeypatch.setattr(ppt_repair, "chat_json", fake_chat)
    pages = [[_para("")] for _ in range(MAX_REPAIR_BLOCKS + 5)]
    _, calls = _repair(pages, pdf=_pdf(MAX_REPAIR_BLOCKS + 5))
    assert len(calls) == MAX_REPAIR_BLOCKS
    assert count["n"] == MAX_REPAIR_BLOCKS


def test_repair_no_defects_makes_no_calls(monkeypatch):
    async def never(messages, provider):  # pragma: no cover
        raise AssertionError("不该被调用")

    monkeypatch.setattr(ppt_repair, "chat_json", never)
    pages = [[_para("正常")]]
    new_pages, calls = _repair(pages)
    assert calls == []
    assert new_pages == pages


def test_crop_failure_skips_vlm_call(monkeypatch):
    """裁不出原图（页码越界）就不外呼——防幻觉：没有区域原图不修"""
    called = {"n": 0}

    async def fake_chat(messages, provider):
        called["n"] += 1
        return _result(), {"content": "修复"}

    monkeypatch.setattr(ppt_repair, "chat_json", fake_chat)
    # PDF 只有 1 页，但块列表有第 2 页 → 页 1 裁图必然失败
    pages = [[_para("")], [_para("")]]
    new_pages, calls = _repair(pages, pdf=_pdf(1))
    assert len(calls) == 2
    assert calls[0].degraded is False  # 页 0 裁图成功，正常修复
    assert calls[1].error == "区域裁剪失败" and calls[1].degraded is True
    assert "repaired" not in new_pages[1][0]
    assert called["n"] == 1


def test_crop_region_returns_png_bytes():
    png = crop_region(_pdf(), 0, [100, 100, 900, 300], 595.0, 842.0)
    assert png is not None
    pix = pymupdf.Pixmap(io.BytesIO(png))
    assert pix.width > 0 and pix.height > 0


def test_crop_region_rejects_degenerate_and_oob():
    assert crop_region(_pdf(), 5, [100, 100, 900, 300], 595.0, 842.0) is None
    assert crop_region(_pdf(), 0, [500, 500, 500, 900], 595.0, 842.0) is None


# ---------- 渲染器标记 ----------
def _slide_texts(data: bytes) -> list[str]:
    prs = Presentation(io.BytesIO(data))
    return [s.text_frame.text for slide in prs.slides for s in slide.shapes if s.has_text_frame]


def test_flow_marks_repaired_block():
    block = _para("修复后的正文。")
    block["repaired"] = True
    texts = _slide_texts(render_blocks_to_pptx([[block]], {}))
    assert "修复后的正文。" in texts
    assert any("LLM 修复" in t for t in texts)


def test_page_mode_marks_repaired_block():
    block = _para("修复后的正文。", bbox=(100, 100, 900, 200))
    block["repaired"] = True
    # 横向页强制 A 模
    texts = _slide_texts(render_blocks_to_pptx([[block]], {}, [(842, 474)]))
    assert "修复后的正文。" in texts
    assert "LLM 修复" in texts


def test_unrepaired_blocks_have_no_marker():
    texts = _slide_texts(render_blocks_to_pptx([[_para("普通正文")]], {}))
    assert not any("LLM 修复" in t for t in texts)
