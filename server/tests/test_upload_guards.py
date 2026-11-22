"""
上传规模上限 与 base_url 的 SSRF 校验

这两道闸都是"给别人用之后才会被触发"的防护，所以判据必须钉死：
- 上限要在**读完之前**就断，否则"防内存打爆"只是自我安慰；
- SSRF 必须解析成 IP 再判，字符串黑名单挡不住 `127.1` / `[::1]` / `0177.0.0.1`。

保持单测边界：不连 Postgres、**不出网**——所以只测字面 IP（getaddrinfo 对数字地址
不发 DNS 查询）与开关早退路径，不去解析真实域名。
"""

import asyncio
import io
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from schemas import provider as provider_schema
from schemas.provider import ProviderCreate, ProviderUpdate
from services import uploads as uploads_module
from services.uploads import check_pdf_pages, read_capped, read_capped_sync


@pytest.fixture(autouse=True)
def _no_limits(monkeypatch):
    """默认给一个宽松但有值的配置，单个用例再自己收窄，避免受本机 .env 影响"""
    fake = SimpleNamespace(max_upload_mb=1, max_pdf_pages=10, allow_private_provider_urls=False)
    monkeypatch.setattr(uploads_module, "get_settings", lambda: fake)
    monkeypatch.setattr(provider_schema, "get_settings", lambda: fake)
    return fake


def _upload(data: bytes) -> UploadFile:
    return UploadFile(filename="x.docx", file=io.BytesIO(data))


def _pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    raw = doc.tobytes()
    doc.close()
    return raw


# ---------- 字节上限 ----------
def test_sync_read_rejects_over_limit_without_draining(monkeypatch, _no_limits):
    """
    超限必须在读到第 limit+1 字节时就抛，而不是先把整份读进内存再判断。
    用计数式 file-like 直接证明它没被读完。
    """
    reads: list[int] = []

    class CountingFile:
        def __init__(self, total: int):
            self.remaining = total

        def read(self, n: int) -> bytes:
            reads.append(n)
            take = min(n, self.remaining)
            self.remaining -= take
            return b"x" * take

    file = UploadFile(filename="big.pdf", file=CountingFile(10 * 1024 * 1024))

    with pytest.raises(HTTPException) as exc:
        read_capped_sync(file)

    assert exc.value.status_code == 413
    # 1 MB 上限 → 最多读到第 2 个 chunk 就该断，绝不读满 10 MB
    assert len(reads) <= 2


def test_sync_read_passes_small_file(_no_limits):
    assert read_capped_sync(_upload(b"abc")) == b"abc"


def test_async_read_matches_sync(_no_limits):
    assert asyncio.run(read_capped(_upload(b"hello"))) == b"hello"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_capped(_upload(b"x" * (2 * 1024 * 1024))))
    assert exc.value.status_code == 413


def test_zero_means_unlimited(monkeypatch, _no_limits):
    monkeypatch.setattr(
        uploads_module,
        "get_settings",
        lambda: SimpleNamespace(max_upload_mb=0, max_pdf_pages=0, allow_private_provider_urls=True),
    )
    data = b"x" * (3 * 1024 * 1024)

    assert len(read_capped_sync(_upload(data))) == len(data)
    check_pdf_pages(_pdf(50))  # 页数上限 0 时任何页数都放过


# ---------- PDF 页数上限 ----------
def test_pdf_over_page_limit_is_400(_no_limits):
    with pytest.raises(HTTPException) as exc:
        check_pdf_pages(_pdf(11))

    assert exc.value.status_code == 400
    assert "11 页" in exc.value.detail
    assert "10 页" in exc.value.detail


def test_pdf_within_limit_passes(_no_limits):
    check_pdf_pages(_pdf(3))


def test_non_pdf_is_ignored(_no_limits):
    """docx 没有"页"的概念，不能拿 PDF 的判据去卡它"""
    check_pdf_pages(b"PK\x03\x04 not a pdf at all")


def test_broken_pdf_is_left_to_the_extractor(_no_limits):
    """
    打不开的文件必须放过：损坏件与加密件在 services/pdf.py 各有专门文案，
    这里抢判据会把"文件已加密"误报成"页数超限"，比不拦更糟。
    """
    check_pdf_pages(b"%PDF-1.7 \x00\x00 garbage that will not parse")


# ---------- base_url SSRF ----------
def test_loopback_is_rejected_by_default(_no_limits):
    with pytest.raises(ValidationError, match="内网或保留地址"):
        ProviderCreate(
            name="local",
            base_url="http://127.0.0.1:19000/v1",
            api_key="sk-1",
            model_name="m",
        )


def test_metadata_endpoint_is_rejected(_no_limits):
    """云上最值钱的一个 SSRF 目标，单独钉住"""
    with pytest.raises(ValidationError, match="内网或保留地址"):
        ProviderCreate(
            name="cloud",
            base_url="http://169.254.169.254/latest/meta-data/",
            api_key="sk-1",
            model_name="m",
        )


def test_ipv6_loopback_is_rejected(_no_limits):
    with pytest.raises(ValidationError, match="内网或保留地址"):
        ProviderCreate(
            name="v6",
            base_url="http://[::1]:19000/v1",
            api_key="sk-1",
            model_name="m",
        )


def test_non_http_scheme_rejected(_no_limits):
    with pytest.raises(ValidationError, match="http:// 或 https://"):
        ProviderCreate(
            name="weird",
            base_url="ftp://example.com/v1",
            api_key="sk-1",
            model_name="m",
        )


def test_missing_host_rejected(_no_limits):
    with pytest.raises(ValidationError, match="缺少主机名"):
        ProviderCreate(
            name="weird",
            base_url="https:///v1",
            api_key="sk-1",
            model_name="m",
        )


def test_dev_opt_in_short_circuits_before_dns(monkeypatch, _no_limits):
    """开了开关就直接放行，不再解析——否则离线环境会卡在 DNS 上"""
    monkeypatch.setattr(
        provider_schema,
        "get_settings",
        lambda: SimpleNamespace(
            max_upload_mb=1, max_pdf_pages=10, allow_private_provider_urls=True
        ),
    )
    p = ProviderCreate(
        name="local",
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-1",
        model_name="m",
    )
    assert p.base_url == "http://127.0.0.1:9/v1"


def test_update_can_omit_base_url_but_rejects_a_bad_one(_no_limits):
    """
    PATCH 语义：不传 base_url 必须照常通过（编辑名字是合法操作）；
    但只要传了就得重过一遍——否则"建号合规、编辑改成内网"就把闸绕过去了。
    """
    assert ProviderUpdate(name="改名").base_url is None

    with pytest.raises(ValidationError, match="内网或保留地址"):
        ProviderUpdate(base_url="http://127.0.0.1:5434/")
