"""
`_mark_done` 单测 —— done 收尾的统一提交点

为什么单独测它：批改 / md / pptx 三条链路的 done 收尾原先各手写一遍五件套，
本轮收成一个入口。入口的价值在于把两条"漏了不报错、只静默给出错数据"的约定
变成不可能漏——`processed_at` 走 DB 时钟、`status` 一定被推进。这两件事必须
有断言钉着，否则下一次加第四条链路时又会手写一遍。

沿用 test_logs_query.py 的约定：内存 SQLite，直接调函数，不连 Postgres 不出网。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1.process import _mark_done
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


def _processing_log(db):
    """造一条正停在 processing 的日志——所有失败/成功路径的起点"""
    u = User(username="alice", password_hash="x")
    db.add(u)
    db.commit()
    log = OperationLog(
        user_id=u.id,
        task_name="作文批改",
        original_filename="a.docx",
        original_content="原文",
        status=LogStatus.PROCESSING,
        needs_ocr=True,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def test_fields_reach_the_database(session):
    """重新查一遍，不靠当前 session 的对象缓存——写进内存不等于写进库"""
    log = _processing_log(session)

    _mark_done(
        session,
        log,
        processed_content="修订后的全文",
        structured_output='{"issues": [], "degraded": false}',
        provider_name="qwen-test",
        needs_ocr=False,
    )
    session.expire_all()
    reloaded = session.get(OperationLog, log.id)

    assert reloaded.status == LogStatus.DONE
    assert reloaded.processed_content == "修订后的全文"
    assert reloaded.provider_name == "qwen-test"
    # needs_ocr 必须由 True 落回 False：留着 True 会让下次重试重复跑 OCR
    assert reloaded.needs_ocr is False


def test_processed_at_uses_db_clock_and_is_not_earlier_than_upload(session):
    """
    时间倒挂是 §7.3 实拍过的缺陷：容器钟比宿主机快 3–4 秒，用应用侧 now() 写
    processed_at 就会出现 processed_at < uploaded_at、前端"处理耗时"是负数。
    这里断言两列都由 DB 生成且顺序正确。
    """
    log = _processing_log(session)
    uploaded_at = log.uploaded_at

    _mark_done(session, log, processed_content="x")

    assert log.processed_at is not None
    assert log.processed_at >= uploaded_at


def test_unknown_field_raises_and_mutates_nothing(session):
    """
    拼错字段名必须响亮失败。更要紧的是"一个都没改"——校验先整体过一遍，
    否则 processed_content 已经写进 session，调用方 rollback 之前那次赋值还在。
    """
    log = _processing_log(session)

    with pytest.raises(ValueError, match="provider_nmae"):
        _mark_done(
            session,
            log,
            processed_content="不该被写进去",
            provider_nmae="拼错了一个字母",
        )

    assert log.processed_content is None
    assert log.status == LogStatus.PROCESSING
    assert log.processed_at is None
