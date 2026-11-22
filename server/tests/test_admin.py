"""
管理员 + 默认模型授权单测（SPEC §9.11 / 任务 2）

覆盖三块：
- provider_usable_by 的授权矩阵（纯函数）；
- resolve_provider 的公共池回落与"未授权即不存在"（内存 SQLite，建 User 行）；
- admin 路由的两道防自锁护栏（直接调路由函数，不起服务）。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1.admin import update_default_access, update_role
from database import Base
from models.provider import ModelProvider
from models.user import User
from schemas.admin import DefaultAccessUpdate, RoleUpdate
from services.llm import LLMError, provider_usable_by, resolve_provider
from services.secrets import encrypt_secret


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def _user(db, name, role="user", can_use_default=False):
    u = User(username=name, password_hash="x", role=role, can_use_default=can_use_default)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _provider(db, owner_id, name, is_public=False, capabilities="text"):
    p = ModelProvider(
        user_id=owner_id,
        name=name,
        base_url="https://example.test/v1",
        api_key=encrypt_secret("sk-1"),
        model_name="m",
        capabilities=capabilities,
        is_public=is_public,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------- provider_usable_by 授权矩阵 ----------
def test_own_provider_always_usable(session):
    u = _user(session, "alice")
    p = _provider(session, u.id, "mine")
    assert provider_usable_by(p, u) is True


def test_public_usable_only_when_granted(session):
    admin = _user(session, "admin", role="admin")
    granted = _user(session, "granted", can_use_default=True)
    plain = _user(session, "plain")
    p = _provider(session, admin.id, "official", is_public=True)

    assert provider_usable_by(p, granted) is True
    assert provider_usable_by(p, plain) is False


def test_others_private_never_usable(session):
    bob = _user(session, "bob")
    alice = _user(session, "alice")
    p = _provider(session, bob.id, "bob-private")
    assert provider_usable_by(p, alice) is False


# ---------- resolve_provider 公共回落 ----------
def test_resolve_falls_back_to_public_when_granted(session):
    admin = _user(session, "admin", role="admin")
    granted = _user(session, "granted", can_use_default=True)
    official = _provider(session, admin.id, "official", is_public=True)

    # 自己没有任何提供商 → 被授权用户回落到公共池
    got = resolve_provider(session, granted.id, None, "text")
    assert got.id == official.id


def test_resolve_no_public_for_ungranted(session):
    admin = _user(session, "admin", role="admin")
    _provider(session, admin.id, "official", is_public=True)
    plain = _user(session, "plain")

    with pytest.raises(LLMError, match="没有支持 text 能力"):
        resolve_provider(session, plain.id, None, "text")


def test_resolve_explicit_public_requires_grant(session):
    admin = _user(session, "admin", role="admin")
    official = _provider(session, admin.id, "official", is_public=True)
    granted = _user(session, "granted", can_use_default=True)
    plain = _user(session, "plain")

    assert resolve_provider(session, granted.id, official.id, "text").id == official.id
    with pytest.raises(LLMError, match="已被删除或不可用"):
        resolve_provider(session, plain.id, official.id, "text")


def test_resolve_own_wins_over_public(session):
    """自己有自己的提供商时，绝不越过自己的去用公共池"""
    admin = _user(session, "admin", role="admin")
    _provider(session, admin.id, "official", is_public=True)
    alice = _user(session, "alice", can_use_default=True)
    own = _provider(session, alice.id, "alice-own")
    assert resolve_provider(session, alice.id, None, "text").id == own.id


# ---------- admin 路由护栏 ----------
def test_cannot_change_own_role(session):
    admin = _user(session, "admin", role="admin")
    with pytest.raises(HTTPException, match="不能修改自己"):
        update_role(admin.id, RoleUpdate(role="user"), db=session, admin=admin)


def test_last_admin_cannot_be_demoted(session):
    admin = _user(session, "admin", role="admin")
    other = _user(session, "other", role="admin")
    # 把 other 降了还剩 admin → 允许
    row = update_role(other.id, RoleUpdate(role="user"), db=session, admin=admin)
    assert row.role == "user"
    # 现在只剩 admin 一人，且不能动自己 → 覆盖两条路径
    with pytest.raises(HTTPException):
        update_role(admin.id, RoleUpdate(role="user"), db=session, admin=admin)


def test_default_access_toggle(session):
    admin = _user(session, "admin", role="admin")
    u = _user(session, "alice")
    row = update_default_access(
        u.id, DefaultAccessUpdate(can_use_default=True), db=session, admin=admin
    )
    assert row.can_use_default is True
    # 授权用户随即可用公共池
    official = _provider(session, admin.id, "official", is_public=True)
    assert resolve_provider(session, u.id, None, "text").id == official.id
