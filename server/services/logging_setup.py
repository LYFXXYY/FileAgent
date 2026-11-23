"""
运行日志的落盘配置

【为什么单独一个模块，而不是写在 main.py 里】
- `main.py` 模块底部有一句裸调用 `_setup_logging()`。测试只要 import 到 main，
  就会改动进程的全局 root logger —— 把构造 handler 这件事抽成纯函数后，单测只测
  返回的 handler，不碰全局状态。
- 路径基准必须和上传落盘那套一致（`services/storage.resolve_dir`，基准 `server/`）。
  按 cwd 解析的话，`run_backend.py`、e2e 自起的后端、独立脚本会各写各的日志文件。

【为什么还要 `attach_uvicorn_logs`】
`uvicorn.run()` 启动时会用它自己的 dictConfig 重配 `uvicorn` / `uvicorn.error` /
`uvicorn.access` 三个 logger，handler 指回 stdout 且 `propagate=False`。所以在
`_setup_logging()` 阶段挂上去的文件 handler 会被冲掉——**访问日志（谁在打这个服务）
必须等应用起来之后再补挂一次**，否则文件里只有业务日志，恰恰少了排障时最想看的部分。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from services.storage import resolve_dir

LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# uvicorn 自己那三个 logger（access 就是"谁访问了什么"的唯一记录）
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def build_file_handler(
    log_file: str, max_bytes: int, backup_count: int
) -> RotatingFileHandler | None:
    """
    构造轮转文件 handler；`log_file` 留空返回 None（= 不落盘，行为与加这个功能之前逐字一致）

    `encoding` 必须显式写 utf-8：Windows 下不传 encoding 时 Python 取 locale 编码（cp936），
    中文日志进文件就是 GBK 字节，grep 与跨机器读出来全是乱码（AGENTS §3.5 那个坑的翻版）。
    """
    if not log_file:
        return None

    path = Path(log_file)
    directory = resolve_dir(str(path.parent))  # 相对路径按 server/ 解析，不看 cwd
    handler = RotatingFileHandler(
        Path(directory) / path.name,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


def attach_uvicorn_logs(handler: logging.Handler | None) -> None:
    """
    把文件 handler 补挂到 uvicorn 的 logger 上（幂等，重复调用不会打两遍）

    uvicorn 的 logger 是 `propagate=False` 的，root 上的 handler 对它们无效，
    所以必须逐个挂；用身份判断而不是标记属性，是为了让同一个 handler 能挂到多个 logger。
    """
    if handler is None:
        return
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        if handler not in uvicorn_logger.handlers:
            uvicorn_logger.addHandler(handler)
