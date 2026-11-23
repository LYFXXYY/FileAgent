"""
运行日志落盘（services/logging_setup.py）单测

这里钉的是四件事，都是"日志能不能用来查问题"的实质：
- 没配 LOG_FILE 时**什么都不写**，行为与加这个功能之前逐字一致；
- 相对路径按 `server/` 解析，不看 cwd（否则三个入口各写一份日志）；
- 中文必须是 **utf-8 字节**（Windows 上默认取 locale = cp936，会写成 GBK 乱码）；
- 同一个 handler 重复挂到 uvicorn 的 logger 上不会打两遍。

不 import main：那会在测试进程里改动全局 root logger。
"""

import logging
from pathlib import Path

import pytest

from services import logging_setup
from services.logging_setup import UVICORN_LOGGERS, attach_uvicorn_logs, build_file_handler


@pytest.fixture()
def fake_logs_dir(tmp_path, monkeypatch):
    """把 resolve_dir 指到 tmp（真实实现会顺手建目录，这里同样要建），避免测试在仓库里写文件"""
    target = tmp_path / "logs"
    target.mkdir()
    monkeypatch.setattr(logging_setup, "resolve_dir", lambda _p: str(target))
    return target


def test_empty_log_file_means_no_file_handler():
    assert build_file_handler("", 5 * 1024 * 1024, 5) is None


def test_relative_path_is_resolved_by_storage_rule(fake_logs_dir):
    handler = build_file_handler("logs/backend.log", 5 * 1024 * 1024, 5)
    assert handler is not None
    assert str(handler.baseFilename).startswith(str(fake_logs_dir))
    assert Path(handler.baseFilename).name == "backend.log"
    handler.close()


def test_rotation_limits_are_applied(fake_logs_dir):
    handler = build_file_handler("logs/backend.log", 1024, 3)
    assert (handler.maxBytes, handler.backupCount) == (1024, 3)
    handler.close()


def test_chinese_log_line_is_utf8_bytes(fake_logs_dir):
    """
    这条是本轮功能存在的理由：日志要能被 grep / 被别的机器读

    Windows 上 `RotatingFileHandler` 不传 encoding 时按 locale（cp936）写，
    中文进文件就成了 GBK 字节，读出来是乱码。
    """
    handler = build_file_handler("logs/backend.log", 1024 * 1024, 1)
    handler.emit(
        logging.LogRecord(
            "fileagent", logging.INFO, __file__, 1, "已从 NO_PROXY 移除裸 [::1]", (), None
        )
    )
    handler.close()

    raw = Path(handler.baseFilename).read_bytes()
    assert "移除裸".encode() in raw
    text = raw.decode("utf-8")
    assert "\ufffd" not in text  # 不是替换字符堆出来的假成功
    assert "已从" in text


def test_attach_is_idempotent(fake_logs_dir):
    handler = build_file_handler("logs/backend.log", 1024 * 1024, 1)
    try:
        attach_uvicorn_logs(handler)
        attach_uvicorn_logs(handler)
        for name in UVICORN_LOGGERS:
            uvicorn_logger = logging.getLogger(name)
            assert uvicorn_logger.handlers.count(handler) == 1
    finally:
        for name in UVICORN_LOGGERS:
            logging.getLogger(name).removeHandler(handler)
        handler.close()


def test_attach_none_is_noop():
    before = {name: list(logging.getLogger(name).handlers) for name in UVICORN_LOGGERS}
    attach_uvicorn_logs(None)
    assert {name: logging.getLogger(name).handlers for name in UVICORN_LOGGERS} == before
