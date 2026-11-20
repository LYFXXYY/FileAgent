"""NO_PROXY 裸 [::1] 归一化单测

httpx 的 trust_env=True 会把 NO_PROXY 条目当 URL 解析，裸 [::1] 直接
InvalidURL: Invalid port。归一化只摘这一种写法，其余语义不动。

⚠ Windows 的 os.environ 键名大小写不敏感：NO_PROXY 与 no_proxy 是同一个键，
不能同时设两个再分别断言，双键用例只在 POSIX 跑。
"""

import os
import sys

import pytest

from config import normalize_no_proxy_env

_IS_WIN = sys.platform == "win32"


def _set_no_proxy(monkeypatch, value, key="NO_PROXY"):
    if _IS_WIN:
        monkeypatch.setenv("NO_PROXY", value)
    else:
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.setenv(key, value)


def test_bare_ipv6_bracket_removed(monkeypatch):
    _set_no_proxy(monkeypatch, "localhost,127.0.0.1,[::1],::1")
    assert normalize_no_proxy_env() == ["NO_PROXY"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"


@pytest.mark.skipif(_IS_WIN, reason="Windows 环境键大小写不敏感，双键无法共存")
def test_lowercase_key_also_handled(monkeypatch):
    _set_no_proxy(monkeypatch, "[::1]", key="no_proxy")
    assert normalize_no_proxy_env() == ["no_proxy"]
    assert os.environ["no_proxy"] == ""


def test_no_change_returns_empty(monkeypatch):
    _set_no_proxy(monkeypatch, "localhost,::1")
    assert normalize_no_proxy_env() == []
    assert os.environ["NO_PROXY"] == "localhost,::1"


def test_idempotent(monkeypatch):
    _set_no_proxy(monkeypatch, "localhost,[::1]")
    assert normalize_no_proxy_env() == ["NO_PROXY"]
    assert normalize_no_proxy_env() == []


def test_unset_env_noop(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    if not _IS_WIN:
        monkeypatch.delenv("no_proxy", raising=False)
    assert normalize_no_proxy_env() == []
