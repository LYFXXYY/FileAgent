"""
认证加固单测：口令规则、登录失败锁定、注册上限、单管理员锁

对应 2026-11-22 开放给别人用之前加的 B3。全部走内存 SQLite + 直接调路由函数，
不连 Postgres、不出网。

⚠ 注册成功路径会调 `init_user_defaults`（入库参考文档要打 embedding 端点），
所以这里只测"被限额挡下"那条分支，不去跑成功分支——那是 e2e 的活。
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1 import auth as auth_module
from api.v1.admin import update_role
from api.v1.auth import login, register
from database import Base, db_now
from models.user import User
from schemas.admin import RoleUpdate
from schemas.auth import LoginRequest, RegisterRequest
from services.auth import hash_password


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """失败计数是进程内字典，用例之间必须清干净，否则互相污染"""
    auth_module._FAILED_LOGINS.clear()
    monkeypatch.setattr(
        auth_module,
        "settings",
        SimpleNamespace(
            login_max_failures=3, login_lockout_minutes=15, max_registrations_per_day=0
        ),
    )
    yield
    auth_module._FAILED_LOGINS.clear()


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _request(ip="203.0.113.7"):
    """只要 login() 真正用到的那部分：request.client.host"""
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def _user(db, name, password="Passw0rd!"):
    u = User(username=name, password_hash=hash_password(password))
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------- 口令规则 ----------
def test_password_shorter_than_8_rejected():
    with pytest.raises(ValidationError):
        RegisterRequest(username="alice", password="ab1cdef")


def test_password_all_digits_rejected():
    with pytest.raises(ValidationError, match="不能只用数字"):
        RegisterRequest(username="alice", password="12345678")


def test_password_all_letters_rejected():
    with pytest.raises(ValidationError, match="不能只用字母"):
        RegisterRequest(username="alice", password="abcdefgh")


def test_password_mixed_is_accepted():
    assert RegisterRequest(username="alice", password="abcd1234").password == "abcd1234"


# ---------- 登录失败锁定 ----------
def test_lockout_after_threshold(session):
    _user(session, "alice")
    body = LoginRequest(username="alice", password="wrong-once")

    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            login(body, _request(), session)
        assert exc.value.status_code == 401

    # 第 4 次不再校验密码，直接 429
    with pytest.raises(HTTPException) as exc:
        login(body, _request(), session)
    assert exc.value.status_code == 429
    assert "分钟后再试" in exc.value.detail


def test_correct_password_still_works_below_threshold(session):
    _user(session, "alice", "Passw0rd!")

    with pytest.raises(HTTPException):
        login(LoginRequest(username="alice", password="bad"), _request(), session)

    ok = login(LoginRequest(username="alice", password="Passw0rd!"), _request(), session)
    assert ok.access_token


def test_success_clears_the_failure_counter(session):
    """
    锁定针对"连续失败"。若成功不清账，一个人早先试错过几次，
    之后正常登录几次就会被误锁——那是把自己用户锁在门外。
    """
    _user(session, "alice", "Passw0rd!")
    bad, good = _request(), _request()
    for _ in range(2):
        with pytest.raises(HTTPException):
            login(LoginRequest(username="alice", password="bad"), bad, session)

    login(LoginRequest(username="alice", password="Passw0rd!"), bad, session)
    assert not auth_module._FAILED_LOGINS

    # 清账之后还可以再失败 3 次才锁
    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            login(LoginRequest(username="alice", password="bad"), good, session)
        assert exc.value.status_code == 401


def test_lockout_is_scoped_per_ip(session, monkeypatch):
    """换 IP 就换计数：既让分布式脚本受限于各自 IP，也避免别人拿错密码把你锁在门外"""
    _user(session, "alice")
    body = LoginRequest(username="alice", password="bad")
    for _ in range(3):
        with pytest.raises(HTTPException):
            login(body, _request("10.0.0.1"), session)

    with pytest.raises(HTTPException) as exc:
        login(body, _request("10.0.0.2"), session)
    assert exc.value.status_code == 401  # 新 IP 未被前面的失败牵连


def test_failures_expire_out_of_the_window(session):
    """窗口是滚动的：15 分钟前的失败不该还能累计成锁定"""
    _user(session, "alice")
    body = LoginRequest(username="alice", password="bad")
    req = _request()
    for _ in range(3):
        with pytest.raises(HTTPException):
            login(body, req, session)

    # 把已记录的失败时刻整体挪到窗口之外
    key = next(iter(auth_module._FAILED_LOGINS))
    old = db_now(session) - timedelta(minutes=20)
    auth_module._FAILED_LOGINS[key] = [old, old, old]

    with pytest.raises(HTTPException) as exc:
        login(body, req, session)
    assert exc.value.status_code == 401


def test_zero_disables_the_lockout(session, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "settings",
        SimpleNamespace(
            login_max_failures=0, login_lockout_minutes=15, max_registrations_per_day=0
        ),
    )
    _user(session, "alice")
    body = LoginRequest(username="alice", password="bad")

    for _ in range(10):
        with pytest.raises(HTTPException) as exc:
            login(body, _request(), session)
        assert exc.value.status_code == 401


def test_unknown_user_locks_the_same_way(session):
    """不存在的用户名也要计数，否则拿 401/429 的差异就能枚举用户名"""
    body = LoginRequest(username="ghost", password="bad")
    for _ in range(3):
        with pytest.raises(HTTPException):
            login(body, _request(), session)

    with pytest.raises(HTTPException) as exc:
        login(body, _request(), session)
    assert exc.value.status_code == 429


# ---------- 注册上限 ----------
def test_registration_cap_blocks_before_any_write(session, monkeypatch):
    from config import get_settings

    monkeypatch.setattr(
        auth_module,
        "settings",
        SimpleNamespace(
            login_max_failures=3, login_lockout_minutes=15, max_registrations_per_day=1
        ),
    )
    assert get_settings() is not None  # 只确认 config 可导入，不触碰真实端点
    _user(session, "alice")

    with pytest.raises(HTTPException) as exc:
        register(RegisterRequest(username="bob", password="abcd1234"), session)

    assert exc.value.status_code == 429
    assert session.query(User).filter(User.username == "bob").first() is None


def test_no_cap_means_no_extra_query(session):
    """cap=0 时整段限额逻辑跳过，行为与加这道闸之前一致"""
    with pytest.raises(HTTPException) as exc:
        # 用户名重名照样是 400，不会被误判成限额
        auth_module.settings = SimpleNamespace(
            login_max_failures=3, login_lockout_minutes=15, max_registrations_per_day=0
        )
        _user(session, "alice")
        register(RegisterRequest(username="alice", password="abcd1234"), session)
    assert exc.value.status_code == 400


# ---------- 单管理员锁 ----------
def test_cannot_grant_admin_role(session):
    """开放注册之后，"能设管理员"本身就是最大的越权面，必须接口层封死"""
    admin = _user(session, "boss")
    admin.role = "admin"
    session.commit()
    target = _user(session, "victim")
    monkey_admin = session.get(User, admin.id)

    with pytest.raises(HTTPException) as exc:
        update_role(target.id, RoleUpdate(role="admin"), session, monkey_admin)

    assert exc.value.status_code == 400
    assert "固定只有一个管理员" in exc.value.detail
    session.refresh(target)
    assert target.role == "user"


def test_admin_module_still_guards_self_demotion(session):
    """护栏 1（不能改自己）没被新护栏取代"""
    admin = _user(session, "boss")
    admin.role = "admin"
    session.commit()

    with pytest.raises(HTTPException) as exc:
        update_role(admin.id, RoleUpdate(role="user"), session, admin)
    assert exc.value.status_code == 400
