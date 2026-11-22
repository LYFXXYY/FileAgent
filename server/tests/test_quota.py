"""
每用户每日处理配额（成本闸）单测

这条闸存在的理由：开放给别人用之后，每一次处理都在花真钱（LLM 按 token、MinerU 按页），
而失败重试会把整条链路再跑一遍。没有这道闸，一个人脚本化点处理就能刷爆账单。

沿用 test_mark_done.py 的约定：内存 SQLite，直接调函数，不连 Postgres 不出网。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1 import process as process_module
from api.v1.process import _quota_error
from database import Base, db_now
from models.operation_log import LogStatus, OperationLog
from models.user import User


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _user(db, name):
    u = User(username=name, password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _log(db, user_id, *, status=LogStatus.DONE, processed_at=None):
    log = OperationLog(
        user_id=user_id,
        task_name="作文批改",
        original_filename="a.docx",
        status=status,
        processed_at=processed_at,
    )
    db.add(log)
    db.commit()
    return log


# ---------- 默认不限制 ----------
def test_zero_means_unlimited(session, monkeypatch):
    """
    默认值 0 必须让这道闸完全不生效——包括"已经跑了几百次"的情况。
    否则一拉代码单机自用就被莫名限流。
    """
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=0))
    alice = _user(session, "alice")
    now = db_now(session)
    for _ in range(30):
        _log(session, alice.id, processed_at=now)

    assert _quota_error(session, alice.id) is None


# ---------- 触发上限 ----------
def test_limit_reached_returns_actionable_text(session, monkeypatch):
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=3))
    alice = _user(session, "alice")
    now = db_now(session)
    for _ in range(3):
        _log(session, alice.id, processed_at=now)

    msg = _quota_error(session, alice.id)

    assert msg is not None
    # 文案要让用户知道"是配额到了"而不是"系统坏了"，并给出出路
    assert "3 次" in msg
    assert "MAX_PROCESSES_PER_DAY" in msg


def test_below_limit_passes(session, monkeypatch):
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=3))
    alice = _user(session, "alice")
    _log(session, alice.id, processed_at=db_now(session))

    assert _quota_error(session, alice.id) is None


# ---------- 计入什么 ----------
def test_pending_and_uploads_do_not_count(session, monkeypatch):
    """上传不花钱，只有真正触发过处理才算成本；pending 行 processed_at 是 NULL"""
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=2))
    alice = _user(session, "alice")
    _log(session, alice.id, status=LogStatus.PENDING, processed_at=None)
    _log(session, alice.id, status=LogStatus.PENDING, processed_at=None)
    _log(session, alice.id, status=LogStatus.PENDING, processed_at=None)

    assert _quota_error(session, alice.id) is None


def test_failed_retries_count(session, monkeypatch):
    """失败重试会再烧一遍钱，必须计入，否则配额可以被"故意跑挂再重试"绕穿"""
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=2))
    alice = _user(session, "alice")
    now = db_now(session)
    _log(session, alice.id, status=LogStatus.FAILED, processed_at=now)
    _log(session, alice.id, status=LogStatus.FAILED, processed_at=now)

    assert _quota_error(session, alice.id) is not None


def test_window_is_rolling_24h(session, monkeypatch):
    """25 小时前的那次不该还占额度——窗口是滚动的，不是按自然日"""
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=1))
    alice = _user(session, "alice")
    old = db_now(session) - timedelta(hours=25)
    _log(session, alice.id, processed_at=old)

    assert _quota_error(session, alice.id) is None


# ---------- 隔离 ----------
def test_other_users_volume_does_not_burn_my_quota(session, monkeypatch):
    """
    §3.1 的隔离铁律在配额上同样成立：按 user_id 计数。
    少了这个条件，别人跑得多我就会先被限流——跨用户互相干扰。
    """
    monkeypatch.setattr(process_module, "settings", SimpleNamespace(max_processes_per_day=2))
    alice = _user(session, "alice")
    bob = _user(session, "bob")
    now = db_now(session)
    for _ in range(10):
        _log(session, bob.id, processed_at=now)

    assert _quota_error(session, alice.id) is None


# ---------- 时钟 ----------
def test_db_now_is_naive_and_parses_sqlite_string(session):
    """
    配额窗口要和 processed_at 同源，所以基准取 DB 时钟而不是应用时钟（§7.3）。
    SQLite 下 CURRENT_TIMESTAMP 回来是字符串，必须归一成 naive datetime，
    否则与 ORM 读出的 datetime 比较会 TypeError。
    """
    now = db_now(session)

    assert isinstance(now, datetime)
    assert now.tzinfo is None
    # 与 UTC 当前时刻应当同量级（SQLite 的 CURRENT_TIMESTAMP 就是 UTC）
    utc_now = datetime.now(UTC).replace(tzinfo=None)
    assert abs((utc_now - now).total_seconds()) < 300
