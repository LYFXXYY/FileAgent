"""
磁盘回收（services/storage.py）单测

这个模块是**会删文件的**，所以测试的重点不是"能删"，而是"不该删的绝不删"：
- 具名真实文档夹具（用户手工放进 uploads/ 的）永远不进计划；
- 还在跑（pending/processing）的日志，其产物永远保留；
- `stored_filename` 为 NULL 的历史行无法回溯，就不该去猜路径删东西。

内存 SQLite + tmp_path，不连 Postgres、不碰真实 uploads/。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.operation_log import LogStatus, OperationLog
from models.user import User
from services.storage import execute, plan_expired, plan_orphans, plan_size, resolve_dir

UUID_NAME = "f" * 32 + ".pdf"
FIXTURE_NAME = "成人肠造口护理指南.pdf"  # 具名夹具：任何情况下都不许进计划


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def dirs(tmp_path):
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    uploads.mkdir()
    (outputs / "pptx").mkdir(parents=True)
    (outputs / "mineru" / "1").mkdir(parents=True)
    (outputs / "mineru" / "1" / "full.md").write_text("x" * 100, encoding="utf-8")
    (uploads / UUID_NAME).write_bytes(b"y" * 500)
    (uploads / FIXTURE_NAME).write_bytes(b"z" * 700)
    (outputs / "pptx" / "1.pptx").write_bytes(b"w" * 300)
    return uploads, outputs


def _log(db, *, status=LogStatus.DONE, age_days=0, stored=UUID_NAME, uploaded_at=None):
    u = User(username="alice", password_hash="x")
    db.add(u)
    db.commit()
    log = OperationLog(
        user_id=u.id,
        task_name="作文批改",
        original_filename="a.pdf",
        status=status,
        stored_filename=stored,
        processed_at=datetime(2026, 1, 1),
        uploaded_at=uploaded_at
        or (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days)),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


# ---------- 过期计划 ----------
def test_zero_days_disables_expiry(session, dirs):
    uploads, outputs = dirs
    _log(session, age_days=999)
    plan = plan_expired(session, uploads, outputs, days=0)
    assert plan.count() == 0
    assert plan.notes  # 要说清"是关着的"，不能让人以为"真的没有可回收的"


def test_old_terminal_log_reclaims_all_three_artifacts(session, dirs):
    uploads, outputs = dirs
    log = _log(session, age_days=40)
    plan = plan_expired(session, uploads, outputs, days=30)

    assert outputs / "pptx" / f"{log.id}.pptx" in plan.files
    assert outputs / "mineru" / str(log.id) in plan.dirs
    assert uploads / UUID_NAME in plan.files


def test_recent_log_is_kept(session, dirs):
    uploads, outputs = dirs
    _log(session, age_days=3)
    assert plan_expired(session, uploads, outputs, days=30).count() == 0


@pytest.mark.parametrize("status", [LogStatus.PENDING, LogStatus.PROCESSING])
def test_in_flight_log_is_never_reclaimed(session, dirs, status):
    """
    哪怕它"过期"了也不能删：还在跑的任务删掉输入原件，等于把一次可能成功的处理判死
    """
    uploads, outputs = dirs
    _log(session, age_days=400, status=status)
    assert plan_expired(session, uploads, outputs, days=30).count() == 0


def test_null_stored_filename_is_not_guessed(session, dirs):
    """历史行没有落盘名就不猜路径——猜错的代价是删掉别人的文件"""
    uploads, outputs = dirs
    log = _log(session, age_days=40, stored=None)
    plan = plan_expired(session, uploads, outputs, days=30)

    assert uploads / UUID_NAME not in plan.files
    assert (outputs / "pptx" / f"{log.id}.pptx") in plan.files  # 产物仍可按 log_id 定位


# ---------- 孤儿计划 ----------
def test_orphan_uuid_file_is_found(session, dirs):
    uploads, _outputs = dirs
    plan = plan_orphans(uploads, session)  # 库里没有任何日志
    assert uploads / UUID_NAME in plan.files


def test_referenced_file_is_not_orphan(session, dirs):
    uploads, _outputs = dirs
    _log(session)
    assert uploads / UUID_NAME not in plan_orphans(uploads, session).files


def test_named_fixture_is_never_touched(session, dirs):
    """
    AGENTS §7.2 的硬约束：用户放进 uploads/ 的具名文件是真实文档夹具。
    它没被任何日志登记（所以是"孤儿"），但形态不匹配 uuid 规则，必须放过。
    """
    uploads, _outputs = dirs
    plan = plan_orphans(uploads, session)
    assert uploads / FIXTURE_NAME not in plan.files


def test_execute_removes_and_reports_bytes(session, dirs):
    uploads, outputs = dirs
    _log(session, age_days=40)
    plan = plan_expired(session, uploads, outputs, days=30)
    expected = plan_size(plan)

    removed, freed = execute(plan)

    assert removed == plan.count()
    assert freed == expected
    assert not (uploads / UUID_NAME).exists()
    assert not (outputs / "mineru" / "1").exists()
    assert (uploads / FIXTURE_NAME).exists()  # 与本次计划无关的文件一个不少


def test_execute_survives_a_locked_file(session, dirs, monkeypatch):
    """回收是尽力而为：一个文件删不掉（被 Word 占用）不该让整个动作失败"""
    uploads, _outputs = dirs
    plan = plan_orphans(uploads, session)

    real_unlink = Path.unlink

    def fake_unlink(self, **kw):
        if self.name == UUID_NAME:
            raise OSError(13, "Permission denied")
        return real_unlink(self, **kw)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    removed, _freed = execute(plan)

    assert removed == 0  # 没删掉的不能虚报


# ---------- 目录解析 ----------
def test_resolve_dir_is_cwd_independent(tmp_path, monkeypatch):
    """
    必须锚在 server/ 而不是 cwd：上传走 run_backend.py、清理走本脚本，
    两者工作目录不同；若按 cwd 解析，清理会扫到一个空目录然后报告"无可回收"。
    """
    monkeypatch.chdir(tmp_path)
    resolved = Path(resolve_dir("uploads_check_probe"))
    assert resolved.is_absolute()
    assert resolved.parent.name == "server"
    assert resolved.exists()
    resolved.rmdir()
