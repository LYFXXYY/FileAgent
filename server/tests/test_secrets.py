"""
api_key 字段级加密单测

最该钉住的是两条边界，它们各自对应一种"看起来能用、其实已经不安全/已经坏了"的状态：
1. **绝不明文落库**——包括编辑路径（PUT），否则创建时加密、改一次就退化成明文；
2. **绝不静默退回明文**——解不开就报错。若为"兼容旧数据"加一条 fallback，
   攻击者往库里写一段明文就能让代码按明文处理，这道闸等于没有。

内存 SQLite + 直接调函数，不连 Postgres、不出网。
"""

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import migrate_encrypt_api_keys as mig
from api.v1.providers import create_provider, update_provider
from config import get_settings
from models.provider import ModelProvider
from models.user import User
from schemas.provider import ProviderCreate, ProviderUpdate
from services.secrets import SecretError, decrypt_secret, encrypt_secret, generate_key, mask_secret

PLAIN = "sk-test-abcdefg1234567"


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    from database import Base

    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def enc_key(monkeypatch):
    """把加密密钥指到一把临时生成的密钥上，不依赖本机 .env"""
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_enc_key", generate_key())
    return settings.api_key_enc_key


# ---------- 加解密本体 ----------
def test_round_trip(enc_key):
    token = encrypt_secret(PLAIN)

    assert token != PLAIN
    assert token.startswith("gAAAAA")  # Fernet token 的固定前缀，迁移脚本靠它分类
    assert decrypt_secret(token) == PLAIN


def test_empty_key_raises_actionably(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_enc_key", "")

    with pytest.raises(SecretError) as exc:
        encrypt_secret(PLAIN)
    assert "API_KEY_ENC_KEY" in str(exc.value)


def test_wrong_key_does_not_decrypt(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_enc_key", generate_key())
    token = encrypt_secret(PLAIN)

    monkeypatch.setattr(settings, "api_key_enc_key", generate_key())
    with pytest.raises(SecretError, match="不是同一把"):
        decrypt_secret(token)


def test_plaintext_cannot_be_decrypted(monkeypatch):
    """没有 fallback 这条路的证据：把明文塞给它不会"将就着用"，而是报错"""
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_enc_key", generate_key())

    with pytest.raises(SecretError):
        decrypt_secret(PLAIN)


# ---------- 展示路径 ----------
def test_mask_shows_prefix_from_ciphertext(enc_key):
    assert mask_secret(encrypt_secret(PLAIN)) == "sk-t****"


def test_mask_never_raises_on_undecryptable(enc_key):
    """列表页不能因为密钥问题整个 500"""
    assert mask_secret("这一行是历史明文") == "****"
    assert mask_secret("") == "****"


# ---------- 路由边界：真正决定"库里躺着什么"----------
def test_create_stores_ciphertext_not_plaintext(session, enc_key):
    u = User(username="alice", password_hash="x")
    session.add(u)
    session.commit()

    resp = create_provider(
        ProviderCreate(
            name="豆包",
            base_url="https://example.invalid/v1",
            api_key=PLAIN,
            model_name="m",
        ),
        session,
        u,
    )

    raw = session.get(ModelProvider, resp.id).api_key
    assert raw != PLAIN
    assert decrypt_secret(raw) == PLAIN
    # 响应里只回掩码，绝不回原文（这条一直有，加密后必须继续成立）
    assert resp.api_key_masked == "sk-t****"
    assert PLAIN not in resp.model_dump_json()


def test_update_with_new_key_stays_encrypted(session, enc_key):
    """
    编辑路径是最容易漏的一半：如果 PUT 直接 setattr 明文，
    那么"创建时加密、改一次密钥就退回明文"，整道闸形同虚设。
    """
    u = User(username="alice", password_hash="x")
    session.add(u)
    session.commit()
    created = create_provider(
        ProviderCreate(
            name="豆包",
            base_url="https://example.invalid/v1",
            api_key=PLAIN,
            model_name="m",
        ),
        session,
        u,
    )

    updated = update_provider(
        created.id,
        ProviderUpdate(api_key="sk-another-secret-999"),
        session,
        u,
    )

    raw = session.get(ModelProvider, created.id).api_key
    assert raw != "sk-another-secret-999"
    assert decrypt_secret(raw) == "sk-another-secret-999"
    assert updated.api_key_masked == "sk-a****"


def test_missing_key_makes_save_fail_loudly(session, monkeypatch):
    """配置不全时是 500 + 可行动文案，而不是把明文写进库"""
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_enc_key", "")
    u = User(username="alice", password_hash="x")
    session.add(u)
    session.commit()

    with pytest.raises(HTTPException) as exc:
        create_provider(
            ProviderCreate(
                name="豆包",
                base_url="https://example.invalid/v1",
                api_key=PLAIN,
                model_name="m",
            ),
            session,
            u,
        )

    assert exc.value.status_code == 500
    assert "E-CONFIG-SECRET" in exc.value.detail
    assert session.query(ModelProvider).count() == 0


# ---------- 迁移脚本的分类判据 ----------
def test_classify_distinguishes_plaintext_from_ciphertext(enc_key):
    assert mig._looks_like_fernet(encrypt_secret(PLAIN)) is True
    assert mig._looks_like_fernet(PLAIN) is False
    assert mig._looks_like_fernet("gAAAAAshort-not-base64") is False


def test_classify_accepts_a_real_fernet_token():
    """不依赖本项目配置：任何合法 Fernet token 都必须被认成密文，否则会被二次加密"""
    token = Fernet.generate_key()
    cipher = Fernet(token)
    assert mig._looks_like_fernet(cipher.encrypt(b"some-real-looking-api-key").decode()) is True
