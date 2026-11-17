"""
PPT 批评循环图（阶段 5 s5.6）单测

不连 Postgres、不出网：HTTP 走假 client 按 system prompt 内容路由——outline / critique /
revise 三类请求各自返回各自形状的响应。同 §3.10 的"假 client 必须按请求内容路由"边界。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_llm import _Response

import services.llm as llm
import services.pptx_renderer as renderer
from graphs.ppt_graph import run_ppt_pipeline


def _provider(name: str = "test-model"):
    from models.provider import ModelProvider

    return ModelProvider(
        id=1,
        user_id=1,
        name=name,
        base_url="https://example.test/v1",
        api_key="sk-1",
        model_name=name,
        capabilities="text",
        max_tokens=4096,
        temperature=0.7,
        is_active=True,
    )


def _vision_provider():
    p = _provider("vision-model")
    p.capabilities = "text,vision"
    p.id = 2
    return p


def _ok(content: str) -> _Response:
    return _Response(
        payload={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "model": "answered-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
    )


def _outline_json(*titles: str) -> str:
    return json.dumps(
        {
            "title": "汇报",
            "slides": [{"title": t, "bullets": ["要点"], "image_placeholders": []} for t in titles],
        },
        ensure_ascii=False,
    )


def _critique_json(verdict: str) -> str:
    issues = (
        [
            {
                "slide_index": 1,
                "category": "title_overlap",
                "severity": "high",
                "description": "标题太大",
                "suggestion": "缩字号",
            }
        ]
        if verdict == "needs_revision"
        else []
    )
    return json.dumps({"verdict": verdict, "issues": issues}, ensure_ascii=False)


class _RouterClient:
    """
    按 messages 里的 system / user 内容判断请求类型

    批评请求 → 看 system 含"视觉评审"
    修订请求 → 看 user 含"按反馈"或"视觉评审反馈"
    大纲请求 → 其他
    """

    def __init__(
        self,
        *,
        outline_titles=("初稿",),
        critique_verdicts=("pass",),
        revised_titles=("终稿",),
    ):
        self.outline_titles = outline_titles
        self.critique_verdicts = list(critique_verdicts)
        self.revised_titles = revised_titles
        self.outline_calls = 0
        self.critique_calls = 0
        self.revise_calls = 0

    async def post(self, url, headers=None, json=None, **ignored):
        msgs = json["messages"] if json else []
        system_text = ""
        user_text = ""
        for m in msgs:
            content = m.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    piece.get("text", "")
                    for piece in content
                    if isinstance(piece, dict) and piece.get("type") == "text"
                )
            else:
                text = ""
            if m.get("role") == "system":
                system_text += text
            else:
                user_text += text

        if "视觉评审专家" in system_text:
            v = self.critique_verdicts[self.critique_calls]
            self.critique_calls += 1
            return _ok(_critique_json(v))

        if "视觉评审反馈" in user_text:
            self.revise_calls += 1
            return _ok(_outline_json(*self.revised_titles))

        self.outline_calls += 1
        # 第一次 plan / 后续 revise 都返回完整 titles，便于断言页数稳定
        return _ok(_outline_json(*self.outline_titles))


@pytest.fixture()
def reset(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_semaphore", None)
    # 图测试永远走 stub 渲染——soffice 真渲染在 test_ppt_render_smoke.py gated 测
    monkeypatch.setattr(
        renderer,
        "get_settings",
        lambda: SimpleNamespace(
            ppt_render_backend="stub", ppt_render_dpi=72, ppt_render_timeout=90.0
        ),
    )
    return monkeypatch


def _run(reset, client, **kw):
    reset.setattr(llm, "get_client", lambda: client)
    return asyncio.run(
        run_ppt_pipeline(
            "源文……",
            _provider(),
            "请提炼：\n{{content}}",
            thread_id="log-1",
            **kw,
        )
    )


# ---------- 基本路径 ----------
def test_pipeline_without_critic_provider_skips_loop(reset):
    """critic_provider=None 走 s5.2 行为：plan → assemble → render → critique(skipped) → END"""
    client = _RouterClient(outline_titles=("页 1", "页 2"))
    result = _run(reset, client)

    assert result.outline.title == "汇报"
    assert len(result.outline.slides) == 2
    assert result.verdict == "skipped"
    assert client.outline_calls == 1
    assert client.critique_calls == 0
    assert client.revise_calls == 0
    # 没有 pptx_bytes 是 bug——assemble 节点应该已产出
    assert len(result.pptx_bytes) > 0


def test_pipeline_with_critic_pass_first_round(reset):
    """critic 第一次说 pass：plan + 1 critique，不进 revise"""
    client = _RouterClient(
        outline_titles=("页 1",),
        critique_verdicts=("pass",),
    )
    result = _run(reset, client, critic_provider=_vision_provider(), max_rounds=2)

    assert result.verdict == "pass"
    assert client.critique_calls == 1
    assert client.revise_calls == 0


def test_pipeline_with_critic_revision_then_pass(reset):
    """critic 第一次挑刺、第二次说 pass：plan + 2 critique + 1 revise"""
    client = _RouterClient(
        outline_titles=("初稿页",),
        critique_verdicts=("needs_revision", "pass"),
        revised_titles=("终稿页",),
    )
    result = _run(reset, client, critic_provider=_vision_provider(), max_rounds=2)

    assert result.verdict == "pass"
    assert client.outline_calls == 1
    assert client.critique_calls == 2
    assert client.revise_calls == 1
    # 修订后的标题应进 outline
    assert result.outline.slides[0].title == "终稿页"


# ---------- 上限与硬钳制 ----------
def test_pipeline_caps_at_max_rounds(reset):
    """max_rounds=1 + 一直挑刺：第一次挑完就不再循环（cap 触发）"""
    client = _RouterClient(
        outline_titles=("页 1",),
        critique_verdicts=("needs_revision",),  # 永远挑刺
    )
    result = _run(reset, client, critic_provider=_vision_provider(), max_rounds=1)

    # verdict 仍是 needs_revision（cap 触发）
    assert result.verdict == "needs_revision"
    assert client.critique_calls == 1
    assert client.revise_calls == 0  # cap 命中，没进 revise


def test_hard_cap_clamps_max_rounds(reset):
    """max_rounds > 5 钳到 5；mock 一直挑刺，应只跑 5 次 critique"""
    client = _RouterClient(
        outline_titles=("页 1",),
        critique_verdicts=("needs_revision",) * 5,
    )
    result = _run(reset, client, critic_provider=_vision_provider(), max_rounds=99)

    assert result.rounds == 5
    assert result.verdict == "needs_revision"


# ---------- 计量 ----------
def test_pipeline_records_phase_specific_purposes(reset):
    """calls 列表里 phase 正确区分 outline / critique / revise"""
    client = _RouterClient(
        outline_titles=("初稿",),
        critique_verdicts=("needs_revision", "pass"),
    )
    result = _run(reset, client, critic_provider=_vision_provider(), max_rounds=2)

    phases = [c.phase for c in result.calls]
    assert "plan_slides" in phases
    assert "critique" in phases
    assert "revise_outline" in phases

    purposes = {c.phase: c.purpose for c in result.calls}
    assert purposes["plan_slides"] == "outline"
    assert purposes["critique"] == "critic"
    assert purposes["revise_outline"] == "outline"


def test_pipeline_purposes_are_degraded_when_parse_falls_back(reset):
    """outline 走降级时 purpose 标 outline_degraded——评测台按这个分组"""
    # 让 outline 响应是自由文本（非 JSON），parsed 是 None
    raw = "这不是 JSON"
    response = _Response(
        payload={
            "choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
            "model": "m",
            "usage": {},
        }
    )

    class _BadOutlineClient:
        async def post(self, url, headers=None, json=None, **ignored):
            return response

    result = _run(reset, _BadOutlineClient())
    plan_call = next(c for c in result.calls if c.phase == "plan_slides")
    assert plan_call.purpose == "outline_degraded"


# ---------- critique 解析降级 ----------
def test_critic_endpoint_garbage_keeps_loop_running(reset):
    """VLM 端吐自由文本：parse_critique 走阶梯，verdict 强制 needs_revision + degraded"""

    class _GarbageCritic:
        async def post(self, url, headers=None, json=None, **ignored):
            content = json["messages"][0]["content"] if json else ""
            # system 是字符串形态（critique 用的是 list 形态，不会撞这里）
            if "视觉评审专家" in content:
                return _ok("抱歉我不会批评")
            return _ok(
                _outline_json("页 1")
            )  # 其他请求按 outline 回，让 cap 早命中（max_rounds=1）

    result = _run(
        reset,
        _GarbageCritic(),
        critic_provider=_vision_provider(),
        max_rounds=1,
    )
    assert result.verdict == "needs_revision"
    critic_call = next(c for c in result.calls if c.phase == "critique")
    assert critic_call.purpose == "critic_degraded"
