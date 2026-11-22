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
