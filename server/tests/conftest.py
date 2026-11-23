"""
测试会话级前置：给 api_key 字段级加密装一把固定的测试密钥

`provider.api_key` 现在必须是 Fernet 密文入库，而 `services.secrets.decrypt_secret`
**刻意不给明文留后路**（解不开就抛）。所以测试里凡是构造 ORM `ModelProvider` 行的地方，
都必须写密文——写明文等于在验证一个生产环境不存在的路径。

密钥固定在 `b"0" * 32` 而不是每次随机生成：随机值会让"同一个会话里先加密后解密"
在某些 fixture 顺序下失效，排错时极难看懂。固定密钥也让失败输出可复现。
"""

import base64

import pytest

from config import get_settings

TEST_FERNET_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """每个用例都装上同一把密钥，用例结束后 monkeypatch 自动还原"""
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_enc_key", TEST_FERNET_KEY)
    yield


@pytest.fixture(autouse=True)
def _ssrf_gate_open(monkeypatch):
    """
    单测里默认放行指向本地的 base_url

    测试构造的 provider 一律写 `http://127.0.0.1:19000/v1` 这种字面地址（不发真实请求，
    见 AGENTS §7.3 的"不出网"边界），而 SSRF 闸默认拒绝私有地址——真实 `.env` 里那行注释掉之后，
    这些用例会整片卡在 `ProviderCreate` 的校验上，报的错还和它们要测的东西毫无关系。
    闸本身由 `tests/test_upload_guards.py` 自己把 `get_settings` 换成 true/false 两臂来测，
    它在自己用例里覆盖本 fixture，互不干扰。
    """
    monkeypatch.setattr(get_settings(), "allow_private_provider_urls", True)
    yield
