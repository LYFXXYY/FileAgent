"""
图状态回收（services/graph_state.py）单测

这里删的是**库里的行**，而且删错了不可恢复（分块缓存没了 = 重试整篇重打、双倍烧 token），
所以测试的重心全在"不该删的绝不进计划"：

- 还在跑（pending / processing）的线程；
- `failed` 但仍在留存窗口内的线程——它是重试省钱的唯一依据；
- 不是 `log-<数字>` / `longdoc.log-<数字>` 形态的线程（将来别的图）。

内存 SQLite + 注入的线程清单：四张 LangGraph 表不在测试库里，
但"哪些 id 可以删"这件事与它们怎么被发现无关，所以分类逻辑照样能钉住。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.operation_log import LogStatus, OperationLog
from models.user import User
from services.graph_state import _thread_log_ids, plan_graph_state, purge_graph_state

NOW = datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _log(db, *, status=LogStatus.DONE, age_days=0, processed_at=..., log_id=None):
    """建一条日志；`processed_at=...` 表示"按 age_days 推算"，传 None 表示历史行没写这个列"""
    user = User(username=f"u{age_days}{status}", password_hash="x")
    db.add(user)
    db.commit()
    log = OperationLog(
        user_id=user.id,
        task_name="作文批改",
        original_filename="a.docx",
        status=status,
        uploaded_at=NOW - timedelta(days=age_days),
        processed_at=(NOW - timedelta(days=age_days)) if processed_at is ... else processed_at,
    )
    if log_id is not None:
        log.id = log_id
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


# ---------- 分类 ----------
def test_done_beyond_retention_is_expired(session):
    log = _log(session, status=LogStatus.DONE, age_days=40)
    plan = plan_graph_state(session, 30, threads={f"log-{log.id}"}, prefixes=set())
    assert plan.expired == [log.id]
    assert plan.orphans == []


def test_in_flight_threads_are_never_planned(session):
    """规矩 1：pending / processing 正在用这份状态，过期窗口到了也不许动"""
    pend = _log(session, status=LogStatus.PENDING, age_days=400)
    proc = _log(session, status=LogStatus.PROCESSING, age_days=400)
    threads = {f"log-{pend.id}", f"log-{proc.id}"}
    plan = plan_graph_state(session, 30, threads=threads, prefixes=set())
    assert plan.log_ids() == []


def test_failed_inside_window_keeps_its_chunk_cache(session):
    """规矩 2：failed 在窗口内必须留着——重试命中按块缓存才不用整篇重打"""
    log = _log(session, status=LogStatus.FAILED, age_days=3)
    plan = plan_graph_state(session, 30, threads={f"log-{log.id}"}, prefixes=set())
    assert plan.log_ids() == []


def test_failed_past_window_is_reclaimable(session):
    log = _log(session, status=LogStatus.FAILED, age_days=45)
    plan = plan_graph_state(session, 30, threads={f"log-{log.id}"}, prefixes=set())
    assert plan.expired == [log.id]


def test_orphan_thread_is_planned_even_when_days_zero(session):
    """日志行已经没了（e2e 删库）→ 图状态指向不存在的对象，与留存期无关"""
    plan = plan_graph_state(session, 0, threads={"log-999"}, prefixes={"longdoc.log-999"})
    assert plan.orphans == [999]
    assert plan.expired == []
    assert any("retention_days=0" in n for n in plan.notes)


def test_zero_days_disables_expiry(session):
    log = _log(session, status=LogStatus.DONE, age_days=400)
    plan = plan_graph_state(session, 0, threads={f"log-{log.id}"}, prefixes=set())
    assert plan.expired == []


def test_foreign_thread_shapes_are_ignored(session):
    """只认 log-<数字> 与 longdoc.log-<数字>；别的图、别的命名一律不进计划"""
    plan = plan_graph_state(
        session,
        30,
        threads={"ppt-thread-1", "log-abc", "log-", "checkpoint:12"},
        prefixes={"pptx.log-7", "longdoc.log-x", "other.log-3"},
    )
    assert plan.log_ids() == []


def test_store_prefix_reveals_thread_without_checkpoint(session):
    """只有按块缓存、checkpoint 已被别处清掉的线程也要能被发现，否则 store 永远留着"""
    log = _log(session, status=LogStatus.DONE, age_days=40)
    plan = plan_graph_state(session, 30, threads=set(), prefixes={f"longdoc.log-{log.id}"})
    assert plan.expired == [log.id]


def test_missing_processed_at_falls_back_to_upload_time(session):
    """历史行 processed_at 为 NULL：退回 uploaded_at 判，不能因为拿不到时间就永远留"""
    log = _log(session, status=LogStatus.DONE, age_days=40, processed_at=None)
    plan = plan_graph_state(session, 30, threads={f"log-{log.id}"}, prefixes=set())
    assert plan.expired == [log.id]


def test_empty_database_reports_nothing(session):
    plan = plan_graph_state(session, 30, threads=set(), prefixes=set())
    assert plan.count() == 0
    assert any("没有" in n for n in plan.notes)


# ---------- id 归并 ----------
def test_thread_log_ids_merges_both_sources():
    ids = _thread_log_ids({"log-1", "log-20", "other-3"}, {"longdoc.log-20", "longdoc.log-7"})
    assert ids == {1, 7, 20}


# ---------- 空计划不碰库 ----------
def test_purge_with_no_ids_touches_nothing(session):
    assert purge_graph_state(session, []) == {}
