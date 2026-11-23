"""
自助改密单测（`api/v1/auth.py:change_password`）

重心只有一个：**改密口不能成为绕过登录锁定的爆破通道**。所以除了常规的成功/失败路径，
这里专门钉三件事——失败计入与登录同一把计数、登录那边锁死了改密也进不来、
成功一次就把账清掉。

内存 SQLite + 直接调路由函数，不连 Postgres、不出网（同 test_auth_guards 的口径）。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1 import auth as auth_module
from api.v1.auth import change_password, login
from database import Base
from models.user import User
from schemas.auth import ChangePasswordRequest, LoginRequest
from services.auth import hash_password, verify_password
from services.errors import CODE_PASSWORD_OLD

OLD = "oldpass123"
NEW = "newpass456"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    auth_module._FAILED_LOGINS.clear()
    monkeypatch.setattr(
        auth_module,
        "settings",
        SimpleNamespace(login_max_failures=3, login_lockout_minutes=15),
    )
    yield
    auth_module._FAILED_LOGINS.clear()


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(username="alice", password_hash=hash_password(OLD))
    db.add(user)
    db.commit()
    db.refresh(user)
    try:
        yield db, user
    finally:
        db.close()


def _request(ip="203.0.113.7"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def _change(db, user, old=OLD, new=NEW, ip="203.0.113.7"):
    body = ChangePasswordRequest(old_password=old, new_password=new)
    return change_password(body=body, request=_request(ip), current=user, db=db)


# ---------- 成功路径 ----------
def test_success_replaces_hash_and_clears_failures(session):
    db, user = session
    auth_module._FAILED_LOGINS[("203.0.113.7", "alice")] = []
    _change(db, user)
    db.refresh(user)
    assert verify_password(NEW, user.password_hash)
    assert not verify_password(OLD, user.password_hash)
    # 清账：改密成功不该让人背着之前的失败记录
    assert ("203.0.113.7", "alice") not in auth_module._FAILED_LOGINS


def test_body_has_no_user_id_field():
    """改的是谁只由 token 决定；请求体里出现 user_id 就是越权口子"""
    assert "user_id" not in ChangePasswordRequest.model_fields


# ---------- 原密码不对 ----------
def test_wrong_old_password_is_counted(session):
    db, user = session
    with pytest.raises(HTTPException) as exc:
        _change(db, user, old="wrong-old-1")
    assert exc.value.status_code == 400
    assert CODE_PASSWORD_OLD in exc.value.detail
    assert len(auth_module._FAILED_LOGINS[("203.0.113.7", "alice")]) == 1


def test_repeated_wrong_old_password_locks(session):
    """连错三次就锁——用的正是登录那把闸，不是另起一套计数"""
    db, user = session
    for _ in range(3):
        with pytest.raises(HTTPException):
            _change(db, user, old="guess-111")
    with pytest.raises(HTTPException) as exc:
        _change(db, user, old=OLD)  # 这次连原密码都对，也该被挡在门外
    assert exc.value.status_code == 429


def test_login_lockout_blocks_change_password_too(session):
    """跨接口验证：爆破登录口把账号锁了，改密口同样进不去"""
    db, user = session
    for _ in range(3):
        with pytest.raises(HTTPException):
            login(
                body=LoginRequest(username="alice", password="bad"),
                request=_request(),
                db=db,
            )
    with pytest.raises(HTTPException) as exc:
        _change(db, user)
    assert exc.value.status_code == 429


def test_different_ip_has_its_own_budget(session):
    """计数键是 (IP, 用户名)：换 IP 就是另一份额度（同登录那条设计）"""
    db, user = session
    for _ in range(3):
        with pytest.raises(HTTPException):
            _change(db, user, old="guess-111", ip="198.51.100.9")
    _change(db, user, ip="203.0.113.7")  # 本机没被打过，照样能改
    db.refresh(user)
    assert verify_password(NEW, user.password_hash)


# ---------- 新密码强度 ----------
@pytest.mark.parametrize("weak", ["12345678", "abcdefgh", "short1"])
def test_weak_or_short_new_password_rejected_before_handler(weak):
    """422 由 pydantic 在进函数体之前就抛掉，处理函数根本不该看到这种请求"""
    with pytest.raises(ValidationError):
        ChangePasswordRequest(old_password=OLD, new_password=weak)


def test_old_password_is_not_strength_checked(session):
    """原密码可能是 6 位历史口令：校验它等于把人挡在门外改不了"""
    db, user = session
    user.password_hash = hash_password("abc123")
    db.commit()
    _change(db, user, old="abc123")
    db.refresh(user)
    assert verify_password(NEW, user.password_hash)


def test_same_as_old_rejected(session):
    db, user = session
    with pytest.raises(HTTPException) as exc:
        _change(db, user, new=OLD)
    assert exc.value.status_code == 400
    assert "不能与原密码相同" in exc.value.detail
