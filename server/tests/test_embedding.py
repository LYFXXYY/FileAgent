"""embedding 传输层单测 —— 配置校验、批量切段、查询前缀、维度门禁

不出网：桩掉 services.embedding._get_client，用假 .post 造响应。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from models.spec_doc import EMBEDDING_DIM
from services import embedding as emb


class _Resp:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """记录每次请求体，按输入条数吐同维向量"""

    def __init__(self, dim=EMBEDDING_DIM, reorder=False, echo=False):
        self.requests: list[dict] = []
        self.dim = dim
        self.reorder = reorder
        self.echo = echo

    def post(self, url, json, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        n = len(json["input"])
        items = [{"index": i, "embedding": [0.1] * self.dim} for i in range(n)]
        if self.reorder:
            items = list(reversed(items))
        if self.echo:
            # 条数少给一条，模拟端点吞了输入
            items = items[:-1]
        return _Resp({"data": items})


def _settings(monkeypatch, **over):
    base = {
        "embedding_base_url": "http://127.0.0.1:11386/v1/",
        "embedding_model": "text-embedding-bge-m3",
        "embedding_dimensions": EMBEDDING_DIM,
        "embedding_api_key": "",
        "embedding_timeout": 60.0,
        "embedding_query_prefix": "为这个句子生成表示以用于检索相关文章：",
    }
    base.update(over)
    monkeypatch.setattr(emb, "get_settings", lambda: SimpleNamespace(**base))
    return base


@pytest.fixture(autouse=True)
def _reset_client():
    emb.close_client()
    yield
    emb.close_client()


# ---------- 配置门禁 ----------
def test_missing_config_fails_loudly(monkeypatch):
    """
    没配端点就报"未配置"，而不是拿默认端口去撞运气。

    本机 LM Studio 端口随启动漂移，任何写死的默认值都可能连上另一个模型，
    那种失败是静默的：向量存进去了，维度还刚好一样。
    """
    _settings(monkeypatch, embedding_base_url="", embedding_model="")
    with pytest.raises(emb.EmbeddingError, match="未配置"):
        emb.embed_texts(["x"])


def test_declared_dimension_must_match_column(monkeypatch):
    """配置说 768、列宽是 1024 —— 写库前就该拦，而不是让 pgvector 在半途报错"""
    _settings(monkeypatch, embedding_dimensions=768)
    with pytest.raises(emb.EmbeddingError, match="列宽"):
        emb.embed_texts(["x"])


# ---------- 正常路径 ----------
def test_embed_texts_no_prefix_and_trailing_slash_normalized(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    vectors = emb.embed_texts(["第一条规范", "第二条规范"])

    assert len(vectors) == 2
    assert all(len(v) == EMBEDDING_DIM for v in vectors)
    # base_url 末尾斜杠不能拼出 //embeddings
    assert fake.requests[0]["url"] == "http://127.0.0.1:11386/v1/embeddings"
    assert fake.requests[0]["json"]["input"] == ["第一条规范", "第二条规范"]
    assert fake.requests[0]["json"]["model"] == "text-embedding-bge-m3"


def test_document_side_never_gets_query_prefix(monkeypatch):
    """
    前缀只加在查询端。文档端也加的话，入库向量就全是"查询表示"，
    相似度整体下降但不会报错——属于典型的"看起来能跑，其实不准"。
    """
    fake = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    emb.embed_texts(["再接再励"])

    assert fake.requests[0]["json"]["input"] == ["再接再励"]


def test_query_side_adds_prefix(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    emb.embed_query("再接再励对不对")

    assert fake.requests[0]["json"]["input"] == [
        "为这个句子生成表示以用于检索相关文章：再接再励对不对"
    ]


def test_batches_are_chunked(monkeypatch):
    """整库回填可能上千块，一次发出去请求体太大；按 32 条切段且顺序保持"""
    fake = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    texts = [f"条款 {i}" for i in range(emb._BATCH_SIZE * 2 + 5)]
    vectors = emb.embed_texts(texts)

    assert len(fake.requests) == 3
    assert [len(r["json"]["input"]) for r in fake.requests] == [32, 32, 5]
    assert len(vectors) == len(texts)


def test_response_reordered_by_index_not_array_order(monkeypatch):
    """OpenAI 约定按 data[i].index 归位；乱序响应若直接取数组顺序，文本与向量会错配"""
    fake = _FakeClient(reorder=True)
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    emb.embed_texts(["甲", "乙", "丙"])

    assert len(fake.requests[0]["json"]["input"]) == 3


def test_empty_input_short_circuits(monkeypatch):
    """空列表不发请求：回填脚本遇到"全部已编码"时靠这条省掉一次外呼"""
    fake = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    assert emb.embed_texts([]) == []
    assert fake.requests == []


# ---------- 失败路径 ----------
def test_wrong_dimension_rejected(monkeypatch):
    """
    端点上挂着 768 维的模型时必须拦下来。

    pgvector 的列宽是硬约束，但拦在这一层比让数据库在半途炸更早、也更可解释。
    """
    fake = _FakeClient(dim=768)
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    with pytest.raises(emb.EmbeddingError, match="768 维"):
        emb.embed_texts(["x"])


def test_count_mismatch_rejected(monkeypatch):
    fake = _FakeClient(echo=True)
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)

    with pytest.raises(emb.EmbeddingError, match="条数对不上"):
        emb.embed_texts(["a", "b"])


def test_http_error_status_message_truncated(monkeypatch):
    class _Boom(_FakeClient):
        def post(self, url, json, headers=None, timeout=None):
            return _Resp(status_code=404, text="model not found " + "x" * 2000)

    monkeypatch.setattr(emb, "_get_client", lambda: _Boom())
    _settings(monkeypatch)

    with pytest.raises(emb.EmbeddingError, match="404") as e:
        emb.embed_texts(["x"])
    assert len(str(e.value)) < 600  # 响应体截断，不把 2000 字塞进日志和前端


def test_transport_error_says_cannot_connect(monkeypatch):
    class _Down:
        def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(emb, "_get_client", lambda: _Down())
    _settings(monkeypatch)

    with pytest.raises(emb.EmbeddingError, match="连不上"):
        emb.embed_texts(["x"])


def test_api_key_header_only_when_configured(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake)
    _settings(monkeypatch)
    emb.embed_texts(["x"])
    assert fake.requests[0]["headers"] == {}

    _settings(monkeypatch, embedding_api_key="sk-local")
    fake2 = _FakeClient()
    monkeypatch.setattr(emb, "_get_client", lambda: fake2)
    emb.embed_texts(["x"])
    assert fake2.requests[0]["headers"] == {"Authorization": "Bearer sk-local"}
