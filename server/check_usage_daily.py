"""一条命令看清"最近这项目被用得怎么样"（只读，不写任何库）。

用法（在 server/ 下）：
    uv run python check_usage_daily.py             # 最近 1 天
    uv run python check_usage_daily.py --days 7    # 最近 N 天

【为什么需要它】开放给别人用之后，第一个问题永远是"今天多少人在用、烧了多少钱、
谁失败了"。这些数据其实**都已经在库里**（`operation_logs` / `llm_calls` / `users`），
缺的只是一次聚合——所以这里不新增表、不埋点，纯查询。

【量不到的东西，如实说明】
- **MinerU 页数**：按页计费，但 `llm_calls` 只记 LLM 外呼，MinerU 刻意不写这张表
  （没有 token 数字可记，AGENTS §3.15）。这里只能给"PDF 处理次数 / 扫描件次数"，
  精确页数得去 MinerU 控制台对账。
- **登录失败的实时计数**：那是进程内字典（§3.22），重启清零、跨进程也读不到，
  本脚本不碰。
"""

import argparse
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import database
from config import get_settings
from database import db_now
from models.llm_call import LlmCall
from models.operation_log import LogStatus, OperationLog
from models.user import User
from services.graph_state import graph_state_sizes
from services.storage import resolve_dir

ERROR_CODE = re.compile(r"（(E-[A-Z-]+)）")


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue  # 正被占用或权限变化：跳过，体检不该因为一个文件失败
    return total


def report(days: int) -> int:
    db = sessionmaker(bind=database.engine)()
    try:
        since = db_now(db) - timedelta(days=days)
        print(f"统计窗口：最近 {days} 天（早于 {since:%Y-%m-%d %H:%M} 不计，DB 时钟为准）\n")

        new_users = db.query(func.count(User.id)).filter(User.created_at >= since).scalar() or 0
        total_users = db.query(func.count(User.id)).scalar() or 0
        print(f"[账号] 窗口内新注册 {new_users}，当前共 {total_users}")

        by_status = dict(
            db.query(OperationLog.status, func.count(OperationLog.id))
            .filter(OperationLog.uploaded_at >= since)
            .group_by(OperationLog.status)
            .all()
        )
        done = by_status.get(LogStatus.DONE, 0)
        failed = by_status.get(LogStatus.FAILED, 0)
        running = by_status.get(LogStatus.PROCESSING, 0) + by_status.get(LogStatus.PENDING, 0)
        rate = (failed / (done + failed) * 100) if (done + failed) else 0.0
        print(
            f"[处理] 共 {done + failed + running} 次：完成 {done}、失败 {failed}"
            f"（失败率 {rate:.1f}%）、进行中 {running}"
        )

        rows = db.execute(
            select(OperationLog.task_name, func.count(OperationLog.id))
            .where(OperationLog.uploaded_at >= since)
            .group_by(OperationLog.task_name)
            .order_by(func.count(OperationLog.id).desc())
            .limit(6)
        ).all()
        if rows:
            print("       按任务：" + "，".join(f"{name or '(已删任务)'} {n}" for name, n in rows))

        messages = db.execute(
            select(OperationLog.error_message).where(
                OperationLog.uploaded_at >= since,
                OperationLog.status == LogStatus.FAILED,
                OperationLog.error_message.is_not(None),
            )
        ).scalars()
        tally: dict[str, int] = {}
        for message in messages:
            found = ERROR_CODE.search(message or "")
            key = found.group(1) if found else "(无编号)"
            tally[key] = tally.get(key, 0) + 1
        if tally:
            top = sorted(tally.items(), key=lambda kv: -kv[1])[:6]
            print("       失败编号：" + "，".join(f"{c} ×{n}" for c, n in top))

        calls = db.execute(
            select(
                LlmCall.purpose,
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.total_tokens), 0),
                func.coalesce(func.avg(LlmCall.latency_ms), 0),
            )
            .where(LlmCall.created_at >= since)
            .group_by(LlmCall.purpose)
        ).all()
        if calls:
            print("[计量] 按用途（次数 / token 合计 / 平均延迟）：")
            for purpose, count, tokens, avg_ms in calls:
                print(
                    f"       {purpose:<18} {count:>4} 次  {int(tokens):>9} tok  {int(avg_ms):>6} ms"
                )
            retried = (
                db.query(func.count(LlmCall.id))
                .filter(LlmCall.created_at >= since, LlmCall.attempts > 1)
                .scalar()
                or 0
            )
            print(f"       重试过的外呼 {retried} 次（attempts>1，说明端点在抖）")
        else:
            print("[计量] 窗口内没有 LLM 外呼")

        pdf_count = (
            db.query(func.count(OperationLog.id))
            .filter(OperationLog.uploaded_at >= since, OperationLog.original_filename.like("%.pdf"))
            .scalar()
            or 0
        )
        ocr_count = (
            db.query(func.count(OperationLog.id))
            .filter(OperationLog.uploaded_at >= since, OperationLog.needs_ocr.is_(True))
            .scalar()
            or 0
        )
        print(
            f"[MinerU] 窗口内 PDF 处理 {pdf_count} 次，其中扫描件 {ocr_count} 次。"
            "⚠ 页数没有入库（按页计费没有 token 可记），精确用量得去 MinerU 控制台对账"
        )

        settings = get_settings()
        uploads = Path(resolve_dir(settings.upload_dir))
        outputs = Path(resolve_dir(settings.output_dir))
        sizes = graph_state_sizes(db)
        graph_mb = sum(sizes.values()) if sizes else 0
        print(
            f"[磁盘] uploads {_mb(_dir_size(uploads))}，outputs {_mb(_dir_size(outputs))}，"
            f"图状态表 {_mb(graph_mb)}（留存期 {settings.retention_days} 天）"
        )
        print(
            f"[配额] 每人每天 {settings.max_processes_per_day} 次、全局注册 "
            f"{settings.max_registrations_per_day} 个/天、PDF 上限 {settings.max_pdf_pages} 页"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="用量与成本体检（只读）")
    parser.add_argument("--days", type=int, default=1, help="统计窗口天数，默认 1")
    args = parser.parse_args()
    sys.exit(report(args.days))
