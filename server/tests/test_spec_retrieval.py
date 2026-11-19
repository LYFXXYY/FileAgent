"""规范检索层单测 —— 条款号解析、滑窗规划、多窗合并、上下文预算

不连 Postgres：`<=>` 是 pgvector 独有算子，SQL 那段留给 e2e 真库跑。
这里覆盖的是纯逻辑——它们错了召回会静默变差，而且没有任何报错信号。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.spec_doc import SpecChunk, SpecDoc
from services.spec_retrieval import (
    SpecHit,
    build_context,
    clause_id,
    excerpt_line,
    fuse_rrf,
    lexical_hits,
    merge_hits,
    plan_windows,
    retrieve_for_document,
)


def _hit(seq: int, text_: str, score: float, doc_id: int = 3) -> SpecHit:
    return SpecHit(doc_id=doc_id, seq=seq, start=0, end=len(text_), text=text_, score=score)


# ---------- 条款号 ----------
def test_clause_id_from_first_line():
    assert clause_id("1.1.1 同音别字\n【适用情形】……") == "1.1.1"


def test_clause_id_when_chapter_header_is_merged_in():
    """split_by_clause 会把章标题并进下一条条款，条款号于是在第 2 行"""
    assert clause_id("第2章 语法类\n2.1.1 的字误用\n【适用情形】") == "2.1.1"


def test_clause_id_ignores_numbers_in_body():
    """
    只看前两行：正文里的"1.5 倍行距""2024 年"不是条款号。

    全篇找数字会把条款识别成行距，引用编号跟着就全是错的。
    """
    assert clause_id("【判定标准】行距 1.5 倍，年份 2024。\n后面还有 3.1.6 的引用") is None


def test_clause_id_falls_back_to_locator():
    """解析不出条款号时给一个 doc-seq 定位符，引用不能是空字符串"""
    hit = _hit(7, "没有编号的一段规范", 0.5)
    assert hit.clause == "doc3-#7"


# ---------- 滑窗 ----------
def test_short_document_is_single_window():
    assert plan_windows("整篇不到预算的文档", 800, 4) == ["整篇不到预算的文档"]


def test_long_document_splits_without_losing_chars():
    body = "甲" * 2000
    windows = plan_windows(body, 800, 8)
    assert "".join(windows) == body  # 覆盖完整，没漏字


def test_oversized_document_samples_across_whole_text():
    """
    窗口数不够用时等步长抽样，必须包含第一段和最后一段。

    只取前 N 段的话，长文档后半部分该命中的条款永远进不来，
    而结果里看不出"是没查到还是根本没查"。
    """
    body = "".join(f"{i:04d}" for i in range(50))  # 200 字，窗口 20 → 10 段
    windows = plan_windows(body, 20, 4)
    assert len(windows) == 4
    assert windows[0] == body[:20]
    assert body[180:200] in windows  # 末段必须在
    assert any(w == body[120:140] for w in windows)  # 中段也抽到了


def test_empty_document_has_no_window():
    assert plan_windows("   ", 800, 4) == []


# ---------- 合并 ----------
def test_merge_dedupes_by_best_score():
    """同一条款命中多个窗口只留最高分，否则 top-k 里实际只有两三条不同条款"""
    batches = [
        [_hit(1, "条款A", 0.40), _hit(2, "条款B", 0.60)],
        [_hit(1, "条款A", 0.75), _hit(3, "条款C", 0.50)],
    ]
    merged = merge_hits(batches, top_k=3)
    assert [(h.seq, h.score) for h in merged] == [(1, 0.75), (2, 0.60), (3, 0.50)]


def test_merge_respects_top_k():
    merged = merge_hits([[_hit(i, f"条款{i}", 0.9 - i * 0.1) for i in range(6)]], top_k=2)
    assert len(merged) == 2
    assert [h.seq for h in merged] == [0, 1]


# ---------- 上下文预算 ----------
def test_context_keeps_highest_scoring_and_reports_dropped():
    """
    超预算丢的是分数低的条款，并且明确写出"还有 N 条未列出"。

    不写这句，模型和用户都以为看到的是完整条款集合。
    """
    hits = [
        _hit(1, "【" + "甲" * 50 + "】", 0.9),
        _hit(2, "【" + "乙" * 50 + "】", 0.8),
        _hit(3, "【" + "丙" * 50 + "】", 0.7),
    ]
    ctx = build_context(hits, max_chars=120)
    assert "甲" in ctx
    assert "丙" not in ctx
    assert "因上下文预算未列出" in ctx


def test_context_empty_hits_is_empty_string():
    assert build_context([], max_chars=100) == ""


# ---------- 编排：滑窗 × 检索 ----------
@pytest.fixture()
def stubbed(monkeypatch):
    """假编码 + 假检索：只验编排，不出网也不碰 pgvector"""
    calls: dict[str, list] = {"embedded": [], "searched": []}

    def fake_embed_query(w):
        calls["embedded"].append(w)
        return [0.0]

    def fake_search(db, *, user_id, vector, limit):
        calls["searched"].append((user_id, limit))
        return [_hit(1, f"1.1.1 命中 {len(calls['searched'])}", 0.5 + len(calls["searched"]) / 100)]

    monkeypatch.setattr("services.spec_retrieval.embed_query", fake_embed_query)
    monkeypatch.setattr("services.spec_retrieval.search_by_vector", fake_search)
    monkeypatch.setattr(
        "services.spec_retrieval.get_settings",
        lambda: SimpleNamespace(
            spec_retrieval_top_k=3,
            spec_retrieval_mode="vector",
            spec_query_window_chars=10,
            spec_query_max_windows=2,
            spec_context_max_chars=3000,
        ),
    )
    return calls


def test_retrieve_windows_then_merges(stubbed):
    retrieve_for_document(None, user_id=17, document_text="甲" * 25)

    assert len(stubbed["embedded"]) == 2  # 25 字 / 窗口 10 = 3 段，钳到 2 个窗口
    assert len(stubbed["searched"]) == 2
    assert all(uid == 17 for uid, _ in stubbed["searched"])  # 每次都带 user_id


def test_retrieve_empty_document_skips_endpoint(stubbed):
    """空文档不发编码请求：省一次外呼，也让"没内容"和"没命中"在日志里分得开"""
    assert retrieve_for_document(None, user_id=17, document_text="") == []
    assert stubbed["embedded"] == []


def test_retrieve_top_k_override(stubbed):
    retrieve_for_document(None, user_id=17, document_text="甲" * 5, top_k=9)
    assert stubbed["searched"][0] == (17, 9)


# ---------- 批改前的编排：四种出口互不混淆 ----------
@pytest.fixture()
def ctx_env(monkeypatch):
    """桩掉开关、库内文档数与检索本身，只验 collect_spec_context 的分支"""
    state = {"rag": True, "docs": 1, "hits": None, "boom": False, "called": 0}

    def fake_retrieve(db, *, user_id, document_text, top_k=None):
        state["called"] += 1
        if state["boom"]:
            raise RuntimeError("连不上 embedding 端点")
        return state["hits"] or []

    monkeypatch.setattr("services.spec_retrieval.retrieve_for_document", fake_retrieve)
    monkeypatch.setattr(
        "services.spec_retrieval.spec_doc_count", lambda db, *, user_id: state["docs"]
    )
    monkeypatch.setattr(
        "services.spec_retrieval.get_settings",
        lambda: SimpleNamespace(
            spec_rag_enabled=state["rag"],
            spec_retrieval_top_k=3,
            spec_retrieval_mode="hybrid",
            spec_query_window_chars=800,
            spec_query_max_windows=4,
            spec_context_max_chars=3000,
        ),
    )
    return state


def _collect():
    from services.spec_retrieval import collect_spec_context

    return collect_spec_context(None, user_id=17, document_text="一段待批改的正文")


def test_disabled_switch_does_not_touch_retrieval(ctx_env):
    ctx_env["rag"] = False
    ctx = _collect()
    assert (ctx.context, ctx.refs, ctx.note) == ("", [], None)
    assert ctx_env["called"] == 0


def test_empty_library_skips_without_note(ctx_env):
    """没建规范库是常态，不该在结果里冒一句提示"""
    ctx_env["docs"] = 0
    ctx = _collect()
    assert (ctx.context, ctx.refs, ctx.note) == ("", [], None)
    assert ctx_env["called"] == 0


def test_endpoint_failure_is_bypass_not_error(ctx_env):
    """
    编码端点不可用 → 批改照跑，但必须留下 note。

    "没查到相关条款"和"查不了"在结果里长得一样就是撒谎（同 §3.7 的 NULL vs []）。
    """
    ctx_env["boom"] = True
    ctx = _collect()
    assert ctx.context == ""
    assert ctx.refs == []
    assert "检索不可用" in ctx.note
    assert ctx_env["called"] == 1


def test_no_hits_gives_no_refs_and_no_note(ctx_env):
    ctx_env["hits"] = []
    ctx = _collect()
    assert (ctx.context, ctx.refs, ctx.note) == ("", [], None)


def test_hits_produce_context_and_traceable_refs(ctx_env):
    ctx_env["hits"] = [
        _hit(0, "1.1.1 同音别字\n【判定标准】替换后语义不通即判定。", 0.5143),
        _hit(20, "3.1.6 成语误用\n【判定标准】核对成语固定写法。", 0.4802),
    ]
    ctx = _collect()

    assert "1.1.1" in ctx.context and "3.1.6" in ctx.context
    assert [r["clause"] for r in ctx.refs] == ["1.1.1", "3.1.6"]
    assert ctx.refs[0]["score"] == 0.5143
    assert ctx.refs[0]["doc_id"] == 3 and ctx.refs[0]["seq"] == 0
    assert ctx.note is None


def test_blank_document_skips_retrieval(ctx_env):
    from services.spec_retrieval import collect_spec_context

    ctx = collect_spec_context(None, user_id=17, document_text="   ")
    assert (ctx.context, ctx.refs, ctx.note) == ("", [], None)
    assert ctx_env["called"] == 0


# ---------- 词法召回（无分词器时的字二元组） ----------


@pytest.fixture()
def spec_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    a = SpecDoc(id=1, user_id=17, title="规范A", source_format=".docx", char_count=9, chunk_count=2)
    b = SpecDoc(
        id=2, user_id=18, title="别人的规范", source_format=".docx", char_count=3, chunk_count=1
    )
    db.add_all([a, b])
    db.add_all(
        [
            SpecChunk(
                doc_id=1,
                seq=0,
                text="1.1.1 同音别字\n【错误示例】他需要再接再励才能取得好成绩。",
                start=0,
                end=30,
            ),
            SpecChunk(
                doc_id=1,
                seq=1,
                text="4.1.3 分号误用\n【判定标准】并列分句之间用分号。",
                start=30,
                end=60,
            ),
            SpecChunk(
                doc_id=2, seq=0, text="9.9.9 无关条款\n再接再励 再接再励 再接再励", start=0, end=20
            ),
        ]
    )
    db.commit()
    try:
        yield db
    finally:
        db.close()


def test_lexical_hits_prefers_shared_error_example(spec_db):
    """文档里出现了某条款举过的错例，词法就该把它顶到第一位"""
    hits = lexical_hits(spec_db, user_id=17, document_text="这事再接再励才行", limit=5)

    assert [h.clause for h in hits] == ["1.1.1"]  # 4.1.3 零重合被丢掉


def test_lexical_hits_respects_user_ownership(spec_db):
    """别人的条款一条都不能出现——词法这一样要带 user_id"""
    hits = lexical_hits(spec_db, user_id=17, document_text="再接再励", limit=5)

    assert all(h.doc_id == 1 for h in hits)
    assert "9.9.9" not in [h.clause for h in hits]


def test_lexical_hits_marks_via_as_lexical(spec_db):
    hits = lexical_hits(spec_db, user_id=17, document_text="再接再励", limit=5)
    assert hits and all(h.via == "lexical" for h in hits)


def test_lexical_hits_empty_document_returns_nothing(spec_db):
    assert lexical_hits(spec_db, user_id=17, document_text="   ", limit=5) == []


# ---------- RRF 融合 ----------
def test_fuse_rrf_uses_ranks_not_raw_scores():
    """
    余弦常见 0.4–0.6、Dice 常见 0.05–0.2，直接相加等于让余弦那一路永远说了算。
    RRF 只看名次，所以词法的第一名能与向量的第一名同台竞争。
    """
    vec = [_hit(0, "条款甲", 0.61), _hit(1, "条款乙", 0.58)]
    lex = [_hit(9, "条款丙", 0.18), _hit(0, "条款甲", 0.12)]

    fused = fuse_rrf({"vector": vec, "lexical": lex}, top_k=2)

    assert [h.seq for h in fused] == [0, 9]  # 两路都命中的条款甲排第一


def test_fuse_rrf_marks_both_when_two_arms_agree():
    vec = [_hit(0, "条款甲", 0.61)]
    lex = [_hit(0, "条款甲", 0.12)]
    fused = fuse_rrf({"vector": vec, "lexical": lex}, top_k=3)

    assert fused[0].via == "both"
    # 保留向量那一份的分数标尺，别把 Dice 悄悄写进余弦的字段里
    assert fused[0].score == 0.61


def test_fuse_rrf_drops_tail_beyond_top_k():
    arms = {"vector": [_hit(i, f"条款{i}", 0.9 - i / 10) for i in range(6)]}
    assert len(fuse_rrf(arms, top_k=3)) == 3


# ---------- 模式派发 ----------
def _mode_stub(monkeypatch, mode):
    calls = {"embed": 0, "vector": 0, "lexical": 0}

    def fake_embed(w):
        calls["embed"] += 1
        return [0.0]

    def fake_search(db, *, user_id, vector, limit):
        calls["vector"] += 1
        return [_hit(1, "1.1.1 向量条款", 0.55)]

    def fake_lexical(db, *, user_id, document_text, limit):
        calls["lexical"] += 1
        return [_hit(2, "2.1.1 词法条款", 0.2)]

    monkeypatch.setattr("services.spec_retrieval.embed_query", fake_embed)
    monkeypatch.setattr("services.spec_retrieval.search_by_vector", fake_search)
    monkeypatch.setattr("services.spec_retrieval.lexical_hits", fake_lexical)
    monkeypatch.setattr(
        "services.spec_retrieval.get_settings",
        lambda: SimpleNamespace(
            spec_retrieval_top_k=2,
            spec_retrieval_mode=mode,
            spec_query_window_chars=800,
            spec_query_max_windows=4,
            spec_context_max_chars=3000,
        ),
    )
    return calls


def test_mode_vector_skips_lexical(monkeypatch):
    calls = _mode_stub(monkeypatch, "vector")
    hits = retrieve_for_document(None, user_id=17, document_text="正文")
    assert calls["lexical"] == 0 and calls["vector"] == 1
    assert [h.seq for h in hits] == [1]


def test_mode_lexical_skips_embedding(monkeypatch):
    """词法模式一次编码都不发：编码端点挂了也能召回，这是混合模式的抗故障来源"""
    calls = _mode_stub(monkeypatch, "lexical")
    hits = retrieve_for_document(None, user_id=17, document_text="正文")
    assert calls["embed"] == 0 and calls["lexical"] == 1
    assert [h.clause for h in hits] == ["2.1.1"]


def test_mode_hybrid_fuses_both_arms(monkeypatch):
    calls = _mode_stub(monkeypatch, "hybrid")
    hits = retrieve_for_document(None, user_id=17, document_text="正文")
    assert calls["embed"] == 1 and calls["lexical"] == 1
    assert {h.seq for h in hits} == {1, 2}


def test_unknown_mode_falls_back_to_hybrid(monkeypatch, caplog):
    calls = _mode_stub(monkeypatch, "tsvector")
    hits = retrieve_for_document(None, user_id=17, document_text="正文")
    assert calls["lexical"] == 1 and len(hits) == 2


def test_clause_id_from_three_line_head():
    """
    真实规范文档的第 0 块：文档标题 + 章标题 + 条款标题，编号在第 3 行。

    只看前两行时它读不出编号，引用会显示成 doc3-#0 这种定位符——
    而模型照着假编号写"（依据 doc3-#0）"，用户看到的溯源就是错的。
    """
    text = "文字编辑校对规范（教学示例稿）\n第1章 错别字类\n1.1.1 同音别字\n【适用情形】读音相同。"
    assert clause_id(text) == "1.1.1"


def test_clause_id_stops_at_body_marker():
    """遇到【…】正文标记就停：正文里的交叉引用不是本块编号"""
    assert clause_id("第1章 错别字类\n【适用情形】参见 3.1.6 的说明。\n3.1.6 成语误用") is None


# ---------- 条款标题行（引用页与交付件都用它） ----------
def test_excerpt_picks_the_clause_heading_line_not_the_doc_title():
    """第 0 块首行是文档标题。取首行会让引用页显示成规范的名字而不是条款名"""
    text = "文字编辑校对规范（教学示例稿）\n第1章 错别字类\n1.1.1 同音别字\n【适用情形】读音相同。"
    assert excerpt_line(text) == "1.1.1 同音别字"


def test_excerpt_falls_back_to_first_line():
    assert excerpt_line("没有编号的一段规范\n正文") == "没有编号的一段规范"


def test_excerpt_of_empty_text_is_empty():
    assert excerpt_line("   ") == ""


def test_refs_carry_excerpt(ctx_env):
    """
    引用里必须带标题行：下载路径只读 structured_output。

    回查规范库会因为库被删改而给出对不上的清单——批完那一刻的依据就丢了。
    """
    ctx_env["hits"] = [_hit(0, "1.1.1 同音别字\n【判定标准】替换后语义不通即判定。", 0.51)]
    from services.spec_retrieval import collect_spec_context

    ctx = collect_spec_context(None, user_id=17, document_text="正文")
    assert ctx.refs[0]["excerpt"] == "1.1.1 同音别字"
