"""MinerU 传输层单测 —— batch 流程、状态机、zip 解包、失败路径

不出网：桩掉 services.mineru._get_client 与 _get_download_client，用假 client 按 URL 路由响应。
轮询里的 time.sleep 也桩掉，超时用例靠 max_wait=0 触发，不真等。
"""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from services import mineru


class _Resp:
    def __init__(self, payload=None, status_code=200, text="", content=b"", bad_json=False):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.content = content
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def _zip_bytes(markdown: str | None = "# 标题\n\n正文第一段。", content_list=None, images=None):
    """按真实产物形状造 zip：full.md / content_list.json / images/"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if markdown is not None:
            zf.writestr("full.md", markdown)
        if content_list is not None:
            zf.writestr("content_list.json", json.dumps(content_list, ensure_ascii=False))
        for name, data in (images or {}).items():
            zf.writestr(f"images/{name}", data)
    return buf.getvalue()


def _ok(data: dict) -> _Resp:
    return _Resp({"code": 0, "msg": "ok", "data": data})


class _FakeClient:
    """按 URL 路由：batch 申请 / PUT 上传 / 轮询 / zip 下载各走各的脚本"""

    def __init__(self, batch=None, put=None, polls=None, zip_resp=None):
        self.calls: list[dict] = []
        self.batch_resp = (
            batch
            if batch is not None
            else _ok({"batch_id": "b-1", "file_urls": ["https://upload.example/put?sig=x"]})
        )
        self.put_resp = put if put is not None else _Resp(status_code=200)
        self.polls = (
            list(polls)
            if polls
            else [
                _ok(
                    {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://cdn.example/z.zip"}
                        ]
                    }
                )
            ]
        )
        self.zip_resp = zip_resp if zip_resp is not None else _Resp(content=_zip_bytes())

    def post(self, url, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return self.batch_resp

    def put(self, url, content=None, headers=None):
        self.calls.append({"method": "PUT", "url": url, "content": content, "headers": headers})
        return self.put_resp

    def get(self, url, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        if url == "https://cdn.example/z.zip":
            return self.zip_resp
        # 轮询脚本耗尽后重复最后一个状态（超时用例靠它停在 running）
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]


def _settings(monkeypatch, **over):
    base = {
        "mineru_token": "test-token",
        "mineru_base_url": "https://mineru.net/api/v4/",
        "mineru_poll_interval": 0.01,
        "mineru_max_wait": 5.0,
        "mineru_http_timeout": 60.0,
    }
    base.update(over)
    monkeypatch.setattr(mineru, "get_settings", lambda: SimpleNamespace(**base))
    return base


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    mineru.close_client()
    monkeypatch.setattr(mineru.time, "sleep", lambda _s: None)
    yield
    mineru.close_client()


def _run(monkeypatch, fake, **over):
    monkeypatch.setattr(mineru, "_get_client", lambda: fake)
    monkeypatch.setattr(mineru, "_get_download_client", lambda: fake)
    _settings(monkeypatch, **over)
    return mineru.parse_pdf_via_mineru(b"%PDF-fake-bytes", "扫描件.pdf")


# ---------- 配置与正常路径 ----------
def test_missing_token_fails_loudly(monkeypatch):
    """没配 token 就响亮报错，而不是静默回空文本让扫描件假装成功"""
    _settings(monkeypatch, mineru_token="")
    with pytest.raises(mineru.MinerUError, match="未配置"):
        mineru.parse_pdf_via_mineru(b"x", "a.pdf")


def test_full_flow_and_request_shapes(monkeypatch):
    fake = _FakeClient(
        polls=[
            _ok({"extract_result": [{"state": "running"}]}),
            _ok(
                {"extract_result": [{"state": "done", "full_zip_url": "https://cdn.example/z.zip"}]}
            ),
        ],
        zip_resp=_Resp(
            content=_zip_bytes(
                markdown="# 第一章\n\n这是正文。",
                content_list=[{"type": "text", "text": "这是正文。"}],
                images={"a.jpg": b"\xff\xd8fake"},
            )
        ),
    )
    result = _run(monkeypatch, fake)

    assert result.markdown == "# 第一章\n\n这是正文。"
    assert result.content_list == [{"type": "text", "text": "这是正文。"}]
    assert result.images == {"a.jpg": b"\xff\xd8fake"}

    post, put, *gets = fake.calls
    # base_url 末尾斜杠不能拼出 //file-urls
    assert post["url"] == "https://mineru.net/api/v4/file-urls/batch"
    assert post["json"]["files"] == [{"name": "扫描件.pdf", "is_ocr": True}]
    assert post["json"]["enable_table"] is True
    assert post["headers"]["Authorization"] == "Bearer test-token"
    # PUT 走预签名地址、带原始字节、不带 Content-Type（签名不含它）
    assert put["url"] == "https://upload.example/put?sig=x"
    assert put["content"] == b"%PDF-fake-bytes"
    assert put["headers"] is None
    # zip 下载不带 Authorization（签名 URL 自带鉴权）
    zip_get = gets[-1]
    assert zip_get["url"] == "https://cdn.example/z.zip"
    assert zip_get["headers"] is None


def test_business_code_error_surfaces_msg(monkeypatch):
    """云 API 会回 HTTP 200 但 code 非 0：两层都要查"""
    fake = _FakeClient(batch=_Resp({"code": 401, "msg": "token 已过期"}))
    with pytest.raises(mineru.MinerUError, match="token 已过期"):
        _run(monkeypatch, fake)


def test_http_error_text_truncated(monkeypatch):
    fake = _FakeClient(batch=_Resp(status_code=500, text="x" * 2000))
    with pytest.raises(mineru.MinerUError, match="HTTP 500") as e:
        _run(monkeypatch, fake)
    assert len(str(e.value)) < 600


def test_non_json_response_rejected(monkeypatch):
    fake = _FakeClient(batch=_Resp(bad_json=True, text="<html>gateway</html>"))
    with pytest.raises(mineru.MinerUError, match="不是 JSON"):
        _run(monkeypatch, fake)


def test_missing_batch_fields_rejected(monkeypatch):
    fake = _FakeClient(batch=_ok({"batch_id": "b-1"}))  # 缺 file_urls
    with pytest.raises(mineru.MinerUError, match="上传地址"):
        _run(monkeypatch, fake)


def test_put_failure_rejected(monkeypatch):
    fake = _FakeClient(put=_Resp(status_code=403))
    with pytest.raises(mineru.MinerUError, match="上传失败"):
        _run(monkeypatch, fake)


# ---------- 轮询状态机 ----------
def test_failed_state_carries_err_msg(monkeypatch):
    fake = _FakeClient(
        polls=[_ok({"extract_result": [{"state": "failed", "err_msg": "页数超出配额"}]})]
    )
    with pytest.raises(mineru.MinerUError, match="页数超出配额"):
        _run(monkeypatch, fake)


def test_timeout_when_never_done(monkeypatch):
    """max_wait=0：第一轮非终态即超时，文案带最后状态供排查"""
    fake = _FakeClient(polls=[_ok({"extract_result": [{"state": "running"}]})])
    with pytest.raises(mineru.MinerUError, match=r"超时.*running"):
        _run(monkeypatch, fake, mineru_max_wait=0.0)


def test_done_without_zip_url_rejected(monkeypatch):
    fake = _FakeClient(polls=[_ok({"extract_result": [{"state": "done"}]})])
    with pytest.raises(mineru.MinerUError, match="full_zip_url"):
        _run(monkeypatch, fake)


def test_zip_download_http_error(monkeypatch):
    fake = _FakeClient(zip_resp=_Resp(status_code=404))
    with pytest.raises(mineru.MinerUError, match="下载失败"):
        _run(monkeypatch, fake)


# ---------- zip 解包 ----------
def test_zip_without_full_md_rejected(monkeypatch):
    fake = _FakeClient(zip_resp=_Resp(content=_zip_bytes(markdown=None)))
    with pytest.raises(mineru.MinerUError, match=r"full\.md"):
        _run(monkeypatch, fake)


def test_zip_slip_entries_skipped(monkeypatch):
    """带 ../ 的条目直接跳过：解包只取 basename，不给路径穿越留口子"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("full.md", "正文")
        zf.writestr("../evil.md", "恶意内容")
        zf.writestr("images/../../evil.jpg", b"x")
        zf.writestr("images/ok.jpg", b"good")
    fake = _FakeClient(zip_resp=_Resp(content=buf.getvalue()))
    result = _run(monkeypatch, fake)
    assert result.markdown == "正文"
    assert result.images == {"ok.jpg": b"good"}


def test_invalid_content_list_degrades_to_none(monkeypatch):
    """content_list 坏了只丢它自己，full.md 还在就能批改"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("full.md", "正文")
        zf.writestr("content_list.json", "{不是合法 JSON")
    fake = _FakeClient(zip_resp=_Resp(content=buf.getvalue()))
    result = _run(monkeypatch, fake)
    assert result.markdown == "正文"
    assert result.content_list is None


def test_content_list_dict_not_list_degrades_to_none(monkeypatch):
    fake = _FakeClient(zip_resp=_Resp(content=_zip_bytes(content_list={"a": 1})))
    result = _run(monkeypatch, fake)
    assert result.content_list is None


# ---------- 真实云 zip 命名（2026-11-20 实测：UUID 前缀 + v2 变体） ----------
def _real_shape_zip(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


_UUID = "c312ff43-49b0-421b-839e-632d50a0d96a"


def test_uuid_prefixed_content_list_matched(monkeypatch):
    """真实云 zip 的 content_list 带任务 UUID 前缀，裸名匹配会整份丢失"""
    cl = [{"type": "text", "text": "正文"}]
    zip_bytes = _real_shape_zip(
        {
            f"{_UUID}_model.json": "{}",
            f"{_UUID}_origin.pdf": b"%PDF",
            f"{_UUID}_content_list.json": json.dumps(cl, ensure_ascii=False),
            "full.md": "# 标题\n\n正文",
            "layout.json": "{}",
            "images/a.jpg": b"\xff\xd8fake",
        }
    )
    fake = _FakeClient(zip_resp=_Resp(content=zip_bytes))
    result = _run(monkeypatch, fake)
    assert result.markdown == "# 标题\n\n正文"
    assert result.content_list == cl
    assert result.images == {"a.jpg": b"\xff\xd8fake"}


def test_content_list_v2_preferred_over_v1(monkeypatch):
    v1 = [{"type": "text", "text": "旧版"}]
    v2 = [{"type": "text", "text": "新版", "bbox": [0, 0, 1, 1]}]
    zip_bytes = _real_shape_zip(
        {
            "full.md": "正文",
            f"{_UUID}_content_list.json": json.dumps(v1, ensure_ascii=False),
            f"{_UUID}_content_list_v2.json": json.dumps(v2, ensure_ascii=False),
        }
    )
    fake = _FakeClient(zip_resp=_Resp(content=zip_bytes))
    assert _run(monkeypatch, fake).content_list == v2


def test_content_list_v2_only(monkeypatch):
    v2 = [{"type": "image", "img_path": "images/a.jpg"}]
    zip_bytes = _real_shape_zip(
        {
            "full.md": "正文",
            f"{_UUID}_content_list_v2.json": json.dumps(v2, ensure_ascii=False),
        }
    )
    fake = _FakeClient(zip_resp=_Resp(content=zip_bytes))
    assert _run(monkeypatch, fake).content_list == v2


def test_broken_v2_falls_back_to_v1(monkeypatch):
    """v2 坏了不连累 v1：降级取能用的那份，而不是整份丢"""
    v1 = [{"type": "text", "text": "可用"}]
    zip_bytes = _real_shape_zip(
        {
            "full.md": "正文",
            f"{_UUID}_content_list.json": json.dumps(v1, ensure_ascii=False),
            f"{_UUID}_content_list_v2.json": "{坏 JSON",
        }
    )
    fake = _FakeClient(zip_resp=_Resp(content=zip_bytes))
    assert _run(monkeypatch, fake).content_list == v1
