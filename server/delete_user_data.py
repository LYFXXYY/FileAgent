"""删除某个账号及其全部数据（运维脚本，管理员界面刻意没有这个动作）。

用法（在 server/ 下）：
    uv run python delete_user_data.py <用户名>            # 只列出会删什么（默认档）
    uv run python delete_user_data.py <用户名> --apply    # 真删

【为什么不做成 admin 界面按钮】AGENTS §3.22 刚把管理员权限面收窄成"只有一个 admin、
不能提升任何人"，而"删掉别人的账号与全部文档"是比改角色更大的破坏面：
一次误点就不可逆。放在服务器端手工执行，等于强制加了一道"操作者人在机器前"的闸。

【外键现实（实测 pg_constraint）】
- `model_providers` / `llm_calls` / `spec_docs` 对 `users` 是 **CASCADE**，删用户会自动带走；
- `tasks` 与 `operation_logs` 对 `users` **没有 ON DELETE 子句 = RESTRICT**，
  所以必须先删这两张表，否则删用户直接报外键错。顺序错了不是"删不干净"，是整件事失败。

【删完还占着磁盘的东西】上传原件、`outputs/pptx/{id}.pptx`、`outputs/mineru/{id}/`、
以及分块批改的图状态线程（`checkpoints*` / `store`）。DB 行删了这些不会跟着走，
所以本脚本一并处理——这正是 §3.25 里"孤儿"的另一个来源。
"""

import argparse
import shutil
import sys
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

import database
from config import get_settings
from models.llm_call import LlmCall
from models.operation_log import OperationLog
from models.provider import ModelProvider
from models.spec_doc import SpecChunk, SpecDoc
from models.task import Task
from models.user import User
from services.graph_state import purge_graph_state
from services.storage import resolve_dir


def _collect(db: sessionmaker, username: str) -> dict | None:
    user = db.execute(select(User).where(User.username == username)).scalars().first()
    if user is None:
        return None

    logs = db.execute(
        select(OperationLog.id, OperationLog.stored_filename).where(OperationLog.user_id == user.id)
    ).all()
    counts = {
        "任务": db.query(func.count(Task.id)).filter(Task.user_id == user.id).scalar(),
        "模型提供商": db.query(func.count(ModelProvider.id))
        .filter(ModelProvider.user_id == user.id)
        .scalar(),
        "操作日志": len(logs),
        "计量行": db.query(func.count(LlmCall.id)).filter(LlmCall.user_id == user.id).scalar(),
        "规范文档": db.query(func.count(SpecDoc.id)).filter(SpecDoc.user_id == user.id).scalar(),
        "规范条款": db.query(func.count(SpecChunk.id))
        .join(SpecDoc, SpecChunk.doc_id == SpecDoc.id)
        .filter(SpecDoc.user_id == user.id)
        .scalar(),
    }
    return {
        "user": user,
        "counts": counts,
        "log_ids": [r.id for r in logs],
        "stored": [r.stored_filename for r in logs if r.stored_filename],
    }


def _guard_last_admin(db, user) -> str | None:
    """护栏：系统里只剩这一个 admin 时不许删——删了没人能管账号，且再也造不出一个"""
    if user.role != "admin":
        return None
    admins = db.query(func.count(User.id)).filter(User.role == "admin").scalar() or 0
    if admins <= 1:
        return f"这是系统里唯一的 admin（共 {admins} 个）。删掉就没人能管理账号了，本脚本拒绝执行"
    return None


def run(username: str, apply_changes: bool) -> int:
    settings = get_settings()
    db = sessionmaker(bind=database.engine)()
    try:
        info = _collect(db, username)
        if info is None:
            print(f"[error] 没有这个用户名：{username}")
            return 2

        user = info["user"]
        if apply_changes:
            blocked = _guard_last_admin(db, user)
            if blocked:
                print(f"[拒绝] {blocked}")
                return 3
        elif user.role == "admin":
            print("[提示] 目标是 admin：--apply 时会检查系统里是否还剩至少一个 admin")

        print(f"账号 {username}（id={user.id}, role={user.role}）")
        for name, value in info["counts"].items():
            print(f"    {name}: {value}")

        uploads_dir = Path(resolve_dir(settings.upload_dir))
        outputs_dir = Path(resolve_dir(settings.output_dir))
        disk_files = [uploads_dir / n for n in info["stored"]]
        disk_files += [outputs_dir / "pptx" / f"{i}.pptx" for i in info["log_ids"]]
        disk_dirs = [outputs_dir / "mineru" / str(i) for i in info["log_ids"]]
        thread_count = len(info["log_ids"])
        print(
            f"    磁盘: {len(disk_files)} 个文件 + {len(disk_dirs)} 个产物目录；"
            f"图状态线程 {thread_count} 个"
        )

        if not apply_changes:
            print("\n[check] 什么都没删。确认清单后加 --apply 执行（**不可恢复**）")
            return 0

        # 顺序：先 RESTRICT 的两张表，再删用户让 CASCADE 收尾
        purge_graph_state(db, info["log_ids"])
        db.execute(text("delete from llm_calls where user_id = :u"), {"u": user.id})
        db.execute(text("delete from operation_logs where user_id = :u"), {"u": user.id})
        db.execute(text("delete from tasks where user_id = :u"), {"u": user.id})
        db.execute(
            text(
                "delete from spec_chunks where doc_id in "
                "(select id from spec_docs where user_id = :u)"
            ),
            {"u": user.id},
        )
        db.execute(text("delete from spec_docs where user_id = :u"), {"u": user.id})
        db.execute(text("delete from model_providers where user_id = :u"), {"u": user.id})
        db.query(User).filter(User.id == user.id).delete()
        db.commit()

        removed = 0
        for p in disk_files + disk_dirs:
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    shutil.rmtree(p)
                    removed += 1
            except OSError:
                continue  # 被 Word 占用就跳过，回收是尽力而为
        print(
            f"[done] 账号与数据已删除；磁盘清理 {removed} 项"
            + ("（有条目被占用没删掉）" if removed < len(disk_files) + len(disk_dirs) else "")
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="删除账号及其全部数据（默认只列清单）")
    parser.add_argument("username")
    parser.add_argument("--apply", action="store_true", help="真删（不可恢复）")
    args = parser.parse_args()
    sys.exit(run(args.username, args.apply))
