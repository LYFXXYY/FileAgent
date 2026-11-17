"""
假 VLM 端点（s5.5）单测 + 批评契约解析

不连 Postgres、不出网。FastAPI TestClient 把 mock 跑起来验响应形状。
"""

import json

import pytest
from fastapi.testclient import TestClient

import test_vlm_server
from schemas.vlm_critique import (
    Critique,
    CritiqueIssue,
    parse_critique,
    serialize_critique,
)


@pytest.fixture()
def client():
    # 每个测试前清状态，避免 _calls_per_thread 跨用例污染
    test_vlm_server._calls_per_thread.clear()
    with TestClient(test_vlm_server.app) as c:
        yield c


def _post(messages, fmt=None):
    body = {"messages": messages}
    if fmt is not None:
        body["response_format"] = fmt
    return body


def _text_critique_request(thread_marker: str) -> dict:
    """模拟批评循环发的一次外呼：text 里带 thread 标记，image_url 占位"""
    return _post(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"批评这一页 {thread_marker}"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ]
    )


# ---------- 形状校验 ----------
def test_response_is_openai_compatible(client):
    r = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:t1]"))
    assert r.status_code == 200
    body = r.json()
    assert "choices" in body
    assert body["model"] == "answered-vlm-model"
    assert "usage" in body


def test_response_content_is_critique_json(client):
    r = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:t1]"))
    content = r.json()["choices"][0]["message"]["content"]
    critique_payload = json.loads(content)
    assert critique_payload["verdict"] in ("pass", "needs_revision")
    assert isinstance(critique_payload["issues"], list)


def test_response_format_unsupported_returns_400(client):
    """与 test_llm_server 同行为：json_schema / json_object 一律 400，逼 chat_json 走降级"""
    r = client.post(
        "/v1/chat/completions",
        json=_post(
            [{"role": "user", "content": "批评 [critic-thread:t1]"}],
            fmt={"type": "json_schema"},
        ),
    )
    assert r.status_code == 400
    assert "response_format" in r.text


# ---------- 退出条件 ----------
def test_stub_rounds_one_yields_issue_then_pass(client, monkeypatch):
    """默认 MOCK_VLM_STUB_ROUNDS=1：第一次挑刺、第二次说 pass"""
    monkeypatch.setattr(test_vlm_server, "_STUB_ROUNDS", 1)

    r1 = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:abc]"))
    p1 = json.loads(r1.json()["choices"][0]["message"]["content"])
    assert p1["verdict"] == "needs_revision"
    assert len(p1["issues"]) >= 1

    r2 = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:abc]"))
    p2 = json.loads(r2.json()["choices"][0]["message"]["content"])
    assert p2["verdict"] == "pass"
    assert p2["issues"] == []


def test_stub_rounds_three_gives_three_chances(client, monkeypatch):
    """调成 3 轮，前 3 次都挑刺，第 4 次才放行"""
    monkeypatch.setattr(test_vlm_server, "_STUB_ROUNDS", 3)

    for _ in range(3):
        r = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:xx]"))
        assert json.loads(r.json()["choices"][0]["message"]["content"])["verdict"] == (
            "needs_revision"
        )

    r = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:xx]"))
    assert json.loads(r.json()["choices"][0]["message"]["content"])["verdict"] == "pass"


def test_threads_are_independent(client, monkeypatch):
    """thread_a 跑完两轮到 pass 不影响 thread_b 还在第一轮"""
    monkeypatch.setattr(test_vlm_server, "_STUB_ROUNDS", 1)

    client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:a]"))
    r_b_first = client.post(
        "/v1/chat/completions", json=_text_critique_request("[critic-thread:b]")
    )
    # b 第一次外呼，仍挑刺
    assert json.loads(r_b_first.json()["choices"][0]["message"]["content"])["verdict"] == (
        "needs_revision"
    )

    r_a_second = client.post(
        "/v1/chat/completions", json=_text_critique_request("[critic-thread:a]")
    )
    assert json.loads(r_a_second.json()["choices"][0]["message"]["content"])["verdict"] == "pass"


def test_reset_endpoint_clears_state(client, monkeypatch):
    monkeypatch.setattr(test_vlm_server, "_STUB_ROUNDS", 1)
    client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:z]"))
    client.post("/v1/_mock/reset")

    # 清完状态后第一次又走"挑刺"分支
    r = client.post("/v1/chat/completions", json=_text_critique_request("[critic-thread:z]"))
    assert json.loads(r.json()["choices"][0]["message"]["content"])["verdict"] == "needs_revision"


def test_missing_thread_marker_uses_anonymous_bucket(client, monkeypatch):
    """prompt 没带 [critic-thread:...] 标记时进 anonymous 桶，仍按 _STUB_ROUNDS 计数"""
    monkeypatch.setattr(test_vlm_server, "_STUB_ROUNDS", 1)

    r1 = client.post("/v1/chat/completions", json=_post([{"role": "user", "content": "看图"}]))
    assert json.loads(r1.json()["choices"][0]["message"]["content"])["verdict"] == (
        "needs_revision"
    )

    r2 = client.post("/v1/chat/completions", json=_post([{"role": "user", "content": "看图"}]))
    assert json.loads(r2.json()["choices"][0]["message"]["content"])["verdict"] == "pass"


# ---------- Critique 解析 ----------
def test_parse_critique_accepts_valid_payload():
    raw = json.dumps(
        {
            "verdict": "needs_revision",
            "issues": [
                {
                    "slide_index": 2,
                    "category": "title_overlap",
                    "severity": "high",
                    "description": "标题压正文",
                    "suggestion": "字号缩 4pt",
                }
            ],
        }
    )
    crit = parse_critique(raw, json.loads(raw))
    assert crit.degraded is False
    assert crit.verdict == "needs_revision"
    assert crit.issues[0].slide_index == 2
    assert crit.issues[0].category == "title_overlap"


def test_parse_critique_pass_with_empty_issues():
    raw = json.dumps({"verdict": "pass", "issues": []})
    crit = parse_critique(raw, json.loads(raw))
    assert crit.verdict == "pass"
    assert crit.issues == []
    assert crit.degraded is False


def test_parse_critique_rejects_unknown_verdict_with_fallback():
    """verdict 取值非 {pass, needs_revision} 时不抛但归到 needs_revision"""
    raw = json.dumps({"verdict": "weird", "issues": []})
    crit = parse_critique(raw, json.loads(raw))
    assert crit.verdict == "needs_revision"


def test_parse_critique_degrades_on_garbage_text():
    """parsed 为 None + raw_text 是自由文本：占位 issue 不假装通过"""
    crit = parse_critique("抱歉我无法批评这张图", None)
    assert crit.degraded is True
    assert crit.verdict == "needs_revision"
    assert len(crit.issues) == 1
    assert "模型未按批评契约输出" in crit.issues[0].description


def test_parse_critique_degrades_on_empty_response():
    """raw 是空串也要 needs_revision + degraded=True——绝不能默认 pass"""
    crit = parse_critique("", None)
    assert crit.verdict == "needs_revision"
    assert crit.degraded is True


def test_critique_serialization_roundtrip():
    crit = Critique(
        verdict="needs_revision",
        issues=[
            CritiqueIssue(
                slide_index=1,
                category="contrast",
                severity="medium",
                description="白底白字",
                suggestion="换深色背景",
            )
        ],
        degraded=False,
    )
    s = serialize_critique(crit)
    back = parse_critique(s, json.loads(s))
    assert back.verdict == "needs_revision"
    assert back.issues[0].category == "contrast"
    assert back.degraded is False
