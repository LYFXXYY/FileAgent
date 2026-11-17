"""
PPT 大纲链路单测 —— schema 校验、解析降级、graph 节点行为

不出网、不连 Postgres：HTTP 用假 client（与 test_llm.py / test_grading_graph.py 同套路），
单节点图不需要 sqlite。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_llm import _Response, _ScriptClient

import services.llm as llm
from graphs import ppt_graph
from graphs.ppt_graph import plan_ppt_outline
from schemas.ppt_outline import (
    PptOutline,
    PptSlide,
    deserialize_outline,
    parse_outline_output,
    serialize_outline,
)


def _provider():
    from models.provider import ModelProvider

    return ModelProvider(
        id=1,
        user_id=1,
        name="t",
        base_url="https://example.test/v1",
        api_key="sk-1",
        model_name="m",
        capabilities="text",
        max_tokens=4096,
        temperature=0.7,
        is_active=True,
    )


# ---------- schema 解析 ----------
def test_parse_valid_outline_no_degrade():
    raw = json.dumps(
        {
            "title": "论文总标题",
            "slides": [
                {
                    "title": "封面",
                    "bullets": ["作者：张三", "学校：XX 大学"],
                    "image_placeholders": [],
                },
                {
                    "title": "研究背景",
                    "bullets": ["问题 1", "问题 2"],
                    "image_placeholders": [],
                },
            ],
        },
        ensure_ascii=False,
    )
    parsed = json.loads(raw)
    outline = parse_outline_output(raw, parsed)

    assert outline.degraded is False
    assert outline.title == "论文总标题"
    assert len(outline.slides) == 2
    assert outline.slides[1].bullets == ["问题 1", "问题 2"]


def test_parse_handles_fenced_json():
    """端点吐 ```json 围栏不算降级——extract_json 已剥壳，parsed 是对的就行"""
    slides = [{"title": "s", "bullets": [], "image_placeholders": []}]
    raw = f"```json\n{json.dumps({'title': 't', 'slides': slides})}\n```"
    parsed = {"title": "t", "slides": slides}
    outline = parse_outline_output(raw, parsed)
    assert outline.degraded is False


def test_parse_falls_back_when_parsed_none():
    """parsed 是 None（端点不支持 response_format 且吐自由文本）走阶梯 2/3"""
    raw = "第一段 重点 A\n第二段 重点 B\n第三段 重点 C"
    outline = parse_outline_output(raw, None)

    assert outline.degraded is True
    assert len(outline.slides) == 3
    assert outline.slides[0].title == "第一段 重点 A"


def test_parse_empty_output_keeps_one_slide():
    """空输出也要给一页占位，绝不让 slides 是空 list——下载层会按页迭代，零页是骗人"""
    outline = parse_outline_output("", None)
    assert outline.degraded is True
    assert len(outline.slides) == 1
    assert "未返回" in outline.slides[0].title or "无效" in outline.slides[0].title


def test_parse_handles_bad_slide_shape_with_fallback():
    """parsed 形状不对（缺 title / bullets 不是 list）退到阶梯 3"""
    raw = "随便写的内容"
    parsed = {"title": "t", "slides": [{"wrong_key": 123}]}
    outline = parse_outline_output(raw, parsed)
    assert outline.degraded is True
    assert outline.slides[0].title.startswith("随便写的内容")


def test_parse_long_line_truncated_to_60_chars():
    """阶梯 3 单行可能上千字，硬切到 60 防 PPT 撑爆"""
    long_line = "长" * 200
    outline = parse_outline_output(long_line, None)
    assert len(outline.slides[0].title) == 60


# ---------- 序列化 roundtrip ----------
def test_serialize_deserialize_roundtrip():
    outline = PptOutline(
        title="t1",
        slides=[PptSlide(title="s1", bullets=["a"], image_placeholders=[])],
        degraded=False,
    )
    s = serialize_outline(outline)
    back = deserialize_outline(s)
    assert back is not None
    assert back.title == "t1"
    assert back.slides[0].bullets == ["a"]
    assert back.degraded is False


def test_deserialize_returns_none_for_grading_payload():
    """批改类 structured_output 不能误被判成 PPT 大纲——kind 字段是判别器"""
    grading_json = json.dumps({"degraded": False, "issues": []})
    assert deserialize_outline(grading_json) is None


def test_deserialize_handles_garbage_input():
    """坏 JSON / None / 空串都返回 None 不抛"""
    assert deserialize_outline(None) is None
    assert deserialize_outline("") is None
    assert deserialize_outline("not json at all") is None


# ---------- graph 节点行为（端到端 mock） ----------
def _ok_outline_payload(title: str, slide_titles: list[str]):
    body = json.dumps(
        {
            "title": title,
            "slides": [{"title": s, "bullets": [], "image_placeholders": []} for s in slide_titles],
        },
        ensure_ascii=False,
    )
    return _Response(
        payload={
            "choices": [{"message": {"content": body}, "finish_reason": "stop"}],
            "model": "answered-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        }
    )


@pytest.fixture()
def reset_globals(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_semaphore", None)
    # 大纲兼容壳也会经过 render 节点，强制 stub 渲染避免被本机 .env 的 soffice 后端拖慢
    import services.pptx_renderer as renderer

    monkeypatch.setattr(
        renderer,
        "get_settings",
        lambda: SimpleNamespace(
            ppt_render_backend="stub", ppt_render_dpi=72, ppt_render_timeout=90.0
        ),
    )
    return monkeypatch


def test_plan_outline_calls_llm_once(reset_globals):
    client = _ScriptClient([_ok_outline_payload("论文", ["封面", "背景", "方法"])])
    reset_globals.setattr(llm, "get_client", lambda: client)

    outline, result = asyncio.run(
        plan_ppt_outline("论文原文……", _provider(), "请提炼：\n{{content}}", thread_id="log-1")
    )

    assert len(client.bodies) == 1
    assert outline.title == "论文"
    assert len(outline.slides) == 3
    assert result is not None and result.total_tokens == 300


def test_plan_outline_handles_endpoint_rejecting_response_format(reset_globals):
    """chat_json 降级路径：先 400 → 不带 response_format 重试 → 拿到自由文本"""
    rejected = _Response(status_code=400, text='{"error":"response_format not supported"}')
    raw_text = "第一段 标题\n第二段 标题"
    fallback = _Response(
        payload={
            "choices": [{"message": {"content": raw_text}, "finish_reason": "stop"}],
            "model": "answered-model",
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }
    )
    client = _ScriptClient([rejected, fallback])
    reset_globals.setattr(llm, "get_client", lambda: client)

    outline, _ = asyncio.run(
        plan_ppt_outline("文档", _provider(), "提炼：{{content}}", thread_id="log-2")
    )

    # 两档外呼 + 阶梯 3 解析 → degraded=True、按行切
    assert len(client.bodies) == 2
    assert outline.degraded is True
    assert len(outline.slides) == 2


def test_plan_outline_raises_on_empty_slides_in_valid_shape(reset_globals):
    """schema 校验过但 slides 空 list——parse 会退到 fallback，不会到这里；
    真到 plan_ppt_outline 这层 slides 还是空，说明图状态被异常改坏，必须明确报错"""
    empty = json.dumps({"title": "x", "slides": []}, ensure_ascii=False)
    response = _Response(
        payload={
            "choices": [{"message": {"content": empty}, "finish_reason": "stop"}],
            "model": "answered-model",
            "usage": {},
        }
    )
    client = _ScriptClient([response])
    reset_globals.setattr(llm, "get_client", lambda: client)

    # parse_outline_output 看到 slides=[] 会走 _fallback_from_text（degraded），
    # fallback 用 raw_text "x" 当 slides[0].title——所以 plan_ppt_outline 不会 raise。
    # 这条测试保证降级链路通畅而不是误抛
    outline, _ = asyncio.run(
        plan_ppt_outline("源文", _provider(), "提炼：{{content}}", thread_id="log-3")
    )
    assert outline.degraded is True
    assert len(outline.slides) >= 1


def test_graph_module_wires_correctly(reset_globals):
    """graphs.ppt_graph 模块导入 + 构造 builder 不应该在 import 期就报错"""
    assert ppt_graph is not None
    builder = ppt_graph._build_graph(_provider(), None, 1, "log-test")
    # s5.6 起图扩展到 5 节点；critique / assemble / render / revise_outline 都在
    nodes = set(builder.nodes)
    assert nodes == {
        "plan_slides",
        "assemble",
        "render",
        "critique",
        "revise_outline",
    }
