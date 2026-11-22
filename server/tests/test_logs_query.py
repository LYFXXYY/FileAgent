"""
日志查询路由单测 —— 分页 / 筛选 / 条数 / 投影

`api/v1/logs.py` 此前完全没有单测覆盖（分页、状态筛选、issue_count 都是裸的），
本轮把"非法状态值静默当不过滤"改成 400 之后，必须有东西钉住两端行为。

沿用 test_admin.py 的约定：直接调路由函数 + 内存 SQLite，不起服务、不连 Postgres。
"""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1.logs import get_log, list_logs
from database import Base
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


def _log(db, owner_id, *, filename="a.docx", status=LogStatus.DONE, structured=None, **kw):
    """造一条日志。正文两列默认塞满，用来验证它们没被拖进列表查询"""
    log = OperationLog(
        user_id=owner_id,
        task_name="作文批改",
        original_filename=filename,
        original_content="原文" * 500,
        processed_content="结果" * 500,
        structured_output=structured,
        status=status,
        **kw,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


# ---------- 数据隔离 ----------
def test_only_own_logs_returned(session):
    mine = _user(session, "mine")
    other = _user(session, "other")
    _log(session, mine.id, filename="我的.docx")
    _log(session, other.id, filename="别人的.docx")

    resp = list_logs(page=1, page_size=20, status=None, filename=None, db=session, current=mine)

    assert resp.total == 1
    assert [i.original_filename for i in resp.items] == ["我的.docx"]


def test_get_log_rejects_other_users_log(session):
    mine = _user(session, "mine")
    other = _user(session, "other")
    secret = _log(session, other.id)

    with pytest.raises(HTTPException) as exc:
        get_log(secret.id, db=session, current=mine)
    # 越权是 404 不是 403：不暴露"资源存在但不属于你"
    assert exc.value.status_code == 404


# ---------- 状态筛选 ----------
def test_status_filter_matches_value_not_name(session):
    u = _user(session, "alice")
    _log(session, u.id, filename="ok.docx", status=LogStatus.DONE)
    _log(session, u.id, filename="bad.docx", status=LogStatus.FAILED)

    resp = list_logs(page=1, page_size=20, status="failed", filename=None, db=session, current=u)

    assert [i.original_filename for i in resp.items] == ["bad.docx"]


def test_invalid_status_is_400_not_silently_unfiltered(session):
    """
    非法值必须报错。静默当"不过滤"的话，用户以为筛了 failed 却拿到全部记录，
    这比报错更坏——而且前端下拉只会送合法值，出这个请求的只能是手改 URL 或脏集成。
    """
    u = _user(session, "alice")
    _log(session, u.id, status=LogStatus.DONE)
    _log(session, u.id, status=LogStatus.FAILED)

    with pytest.raises(HTTPException) as exc:
        list_logs(page=1, page_size=20, status="bogus", filename=None, db=session, current=u)

    assert exc.value.status_code == 400
    assert "bogus" in exc.value.detail


def test_empty_status_means_no_filter(session):
    u = _user(session, "alice")
    _log(session, u.id, status=LogStatus.DONE)
    _log(session, u.id, status=LogStatus.PENDING)

    resp = list_logs(page=1, page_size=20, status="", filename=None, db=session, current=u)

    assert resp.total == 2


# ---------- 文件名搜索 ----------
def test_filename_search_is_substring(session):
    u = _user(session, "alice")
    _log(session, u.id, filename="护理指南.docx")
    _log(session, u.id, filename="论文.docx")

    resp = list_logs(page=1, page_size=20, status=None, filename="护理", db=session, current=u)

    assert [i.original_filename for i in resp.items] == ["护理指南.docx"]


# ---------- 分页 ----------
def test_pagination_ordering_and_total(session):
    u = _user(session, "alice")
    for i in range(5):
        _log(session, u.id, filename=f"f{i}.docx")

    p1 = list_logs(page=1, page_size=2, status=None, filename=None, db=session, current=u)
    p3 = list_logs(page=3, page_size=2, status=None, filename=None, db=session, current=u)

    assert p1.total == 5
    assert len(p1.items) == 2
    assert len(p3.items) == 1
    # 同秒入库时按 id 倒序兜底，两次分页不能出现同一条
    assert set(i.id for i in p1.items).isdisjoint({i.id for i in p3.items})


# ---------- issue_count ----------
def test_issue_count_from_envelope(session):
    u = _user(session, "alice")
    envelope = json.dumps(
        {"issues": [{"type": "typo", "original": "的"}, {"type": "grammar"}], "degraded": False}
    )
    _log(session, u.id, structured=envelope)

    resp = list_logs(page=1, page_size=20, status=None, filename=None, db=session, current=u)

    assert resp.items[0].issue_count == 2


def test_null_envelope_counts_zero(session):
    """NULL = 非批改任务或历史数据。这里必须是 0，而不是报错"""
    u = _user(session, "alice")
    _log(session, u.id, structured=None)

    resp = list_logs(page=1, page_size=20, status=None, filename=None, db=session, current=u)

    assert resp.items[0].issue_count == 0


def test_malformed_envelope_counts_zero_not_500(session):
    u = _user(session, "alice")
    _log(session, u.id, structured="{这不是合法 JSON")

    resp = list_logs(page=1, page_size=20, status=None, filename=None, db=session, current=u)

    assert resp.items[0].issue_count == 0


# ---------- 投影 ----------
def test_response_never_carries_document_text(session):
    """
    列表查询把正文两列 defer 掉了，响应模型本来也不含它们。
    这条钉的是"没人在序列化时把它们 lazy 拉回来"——一旦有人往 LogResponse 里加
    这两个字段，defer 立刻失效而且毫无信号，所以用响应字段集合当断言。
    """
    u = _user(session, "alice")
    _log(session, u.id)

    resp = list_logs(page=1, page_size=20, status=None, filename=None, db=session, current=u)
    payload = resp.items[0].model_dump()

    assert "original_content" not in payload
    assert "processed_content" not in payload
    # 但标量列一个都不能少，defer 不该把它们一起丢掉
    for field in ("id", "original_filename", "status", "uploaded_at", "task_name"):
        assert field in payload
