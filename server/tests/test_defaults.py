"""注册默认配置单测 —— 4 任务 + 通用参考文档；旁路与幂等。

内存 SQLite + 桩编码，不出网。真 embedding 端点缺席时注册不能被拖崩，
这是 services/defaults.py 的两条硬约束之一。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.spec_doc import SpecDoc
from models.task import Task
from models.user import User
from services.defaults import DEFAULT_TASKS, init_user_defaults


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    user = User(username="newbie", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    try:
        yield db, user
    finally:
        db.close()


@pytest.fixture()
def fake_embedder(monkeypatch):
    def _fake(texts):
        return [[float(len(t)), 0.5] for t in texts]

    monkeypatch.setattr("services.spec_storage.embed_texts", _fake)


def _names(db):
    return {t.name for t in db.query(Task).all()}


def test_creates_four_default_tasks(session, fake_embedder):
    db, user = session
    init_user_defaults(db, user)
    assert _names(db) == {s["name"] for s in DEFAULT_TASKS}
    by_name = {t.name: t for t in db.query(Task).all()}
    assert by_name["作文批改"].is_grading is True
    assert by_name["作文批改"].output_format == "docx"
    assert by_name["PDF 转 PPT"].output_format == "pptx"
    assert by_name["文档转 Markdown"].output_format == "md"
    assert by_name["文本润色"].provider_id is None


def test_spec_fixture_ingested(session, fake_embedder):
    db, user = session
    init_user_defaults(db, user)
    docs = db.query(SpecDoc).filter(SpecDoc.user_id == user.id).all()
    assert len(docs) == 1
    assert docs[0].title == "文字编辑校对规范（教学示例稿）"
    assert docs[0].chunk_count > 0


def test_idempotent_second_run(session, fake_embedder):
    db, user = session
    init_user_defaults(db, user)
    init_user_defaults(db, user)
    assert db.query(Task).count() == len(DEFAULT_TASKS)
    assert db.query(SpecDoc).count() == 1


def test_existing_task_not_overwritten(session, fake_embedder):
    db, user = session
    db.add(
        Task(
            user_id=user.id,
            name="作文批改",
            prompt_template="我自己的提示词{{content}}",
            is_grading=True,
        )
    )
    db.commit()
    init_user_defaults(db, user)
    assert db.query(Task).count() == len(DEFAULT_TASKS)
    mine = db.query(Task).filter(Task.name == "作文批改").one()
    assert mine.prompt_template == "我自己的提示词{{content}}"


def test_embedding_down_does_not_block_tasks(session, monkeypatch):
    """显式桩编码端点抛错（保持单测不出网边界）→ 参考文档跳过，任务照建、init 不抛"""
    db, user = session

    def _down(texts):
        raise RuntimeError("embedding 端点未配置/未启动")

    monkeypatch.setattr("services.spec_storage.embed_texts", _down)
    init_user_defaults(db, user)
    assert db.query(Task).count() == len(DEFAULT_TASKS)
    assert db.query(SpecDoc).count() == 0


def test_missing_fixture_bypassed(session, fake_embedder, monkeypatch):
    db, user = session
    monkeypatch.setattr("services.defaults._fixture_path", lambda: "no/such/file.docx")
    init_user_defaults(db, user)
    assert db.query(Task).count() == len(DEFAULT_TASKS)
    assert db.query(SpecDoc).count() == 0
