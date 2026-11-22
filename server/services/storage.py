r"""
磁盘回收：过期产物清理 + 孤儿文件识别

【为什么需要】
`uploads/` 与 `outputs/` 只增不减，开放给别人用之后就是迟早撑爆磁盘的定时炸弹。
但**不能无脑按时间删文件**——有几类产物正在被下载链路依赖：

- 批改/普通 docx：**不占磁盘**，下载时由 `create_diff_docx(original_content, processed_content)`
  从库里现算，所以永远不需要 TTL；
- `outputs/pptx/{log_id}.pptx`：pptx 任务的交付二进制，下载直接读它；
- `outputs/mineru/{log_id}/`：md 任务带图时打 zip 的图片来源；
- `uploads/{stored_filename}`：只在"扫描件重试要重新 OCR""重跑转换"时被读回。

【两条安全规矩】
1. **只删 DB 指向得上的文件**。过期清理按日志行找产物；孤儿清理只认
   `^[0-9a-f]{32}\.(docx|pdf)$` 这种上传落盘形态——用户手工放进 `uploads/` 的
   具名真实文档夹具永不匹配，因此永不会被删（AGENTS §7.2 的硬约束）。
2. **不碰还在跑的日志**。pending / processing 的产物一律保留，哪怕它"过期"了——
   那说明有任务正在用它，删了就是把一次可能成功的处理判死。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import db_now
from models.operation_log import LogStatus, OperationLog

# 上传落盘名：uuid4().hex + 扩展名。孤儿判定只认这个形态，别的文件一概不动
STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(docx|pdf)$")

# 终态：处理已经结束，产物最多用于下载或重试
TERMINAL = (LogStatus.DONE, LogStatus.FAILED)

# services/storage.py 在 server/services/ 下，往上两级才是 server/
_SERVER_ROOT = Path(__file__).resolve().parent.parent


def resolve_dir(subdir: str) -> str:
    """
    把配置里的相对路径解析成绝对路径，并确保存在

    基准是 **server/ 目录**（本文件的上两级），不是进程 cwd。这一点必须和上传链路一致：
    `run_backend.py`、e2e 脚本、pytest 的工作目录各不相同，用 cwd 解析会让同一个
    `UPLOAD_DIR=uploads` 在不同入口下指向不同目录——上传落在一处、清理扫到另一处，
    结果是"清理永远扫不到东西"这种最难发现的失效。
    """
    path = Path(subdir) if Path(subdir).is_absolute() else _SERVER_ROOT / subdir
    path = Path(os.path.normpath(path))
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


@dataclass
class Plan:
    """一次回收的完整计划。分开算"要删什么"和"真去删"，才敢在未审阅前跑 --check"""

    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_file(self, path: Path) -> None:
        if path not in self.files:
            self.files.append(path)

    def add_dir(self, path: Path) -> None:
        if path not in self.dirs:
            self.dirs.append(path)

    def count(self) -> int:
        return len(self.files) + len(self.dirs)


def plan_size(plan: Plan) -> int:
    total = 0
    for p in plan.files:
        if p.is_file():
            total += p.stat().st_size
    for d in plan.dirs:
        if d.is_dir():
            for root, _dirs, files in os.walk(d):
                for f in files:
                    fp = Path(root) / f
                    if fp.is_file():
                        total += fp.stat().st_size
    return total


def plan_expired(
    db: Session,
    uploads_dir: Path,
    outputs_dir: Path,
    days: int,
) -> Plan:
    """
    列出"终态且上传时间超过 days 天"的日志所对应的磁盘产物

    用 `uploaded_at` 而不是 `processed_at` 作基准：留存期对用户的心智是
    "我传的东西你留多久"，而 failed 的行可能压根没 processed_at。
    """
    plan = Plan()
    if days <= 0:
        plan.notes.append("retention_days=0，跳过过期清理")
        return plan

    cutoff = db_now(db) - timedelta(days=days)
    stale = (
        db.query(OperationLog)
        .filter(OperationLog.uploaded_at < cutoff, OperationLog.status.in_(list(TERMINAL)))
        .all()
    )
    for log in stale:
        plan.add_file(outputs_dir / "pptx" / f"{log.id}.pptx")
        plan.add_dir(outputs_dir / "mineru" / str(log.id))
        # 只有记录过落盘名的行才回收得到原文件；历史行该列为 NULL，无法回溯就不删
        if log.stored_filename:
            plan.add_file(uploads_dir / log.stored_filename)
    plan.notes.append(f"过期日志 {len(stale)} 条（早于 {cutoff:%Y-%m-%d}）")
    return plan


def plan_orphans(uploads_dir: Path, db: Session) -> Plan:
    """
    列出 `uploads/` 里没有任何日志指向的落盘文件

    这是历史 e2e 的真正遗留：脚本会删自己的 DB 行，但早期版本的它删不掉文件
    （没有 `stored_filename` 可回溯），于是每次跑完都攒下一批 uuid 命名的垃圾。
    """
    plan = Plan()
    if not uploads_dir.is_dir():
        plan.notes.append("uploads 目录不存在，跳过孤儿扫描")
        return plan

    referenced = set(
        db.execute(
            select(OperationLog.stored_filename).where(OperationLog.stored_filename.is_not(None))
        )
        .scalars()
        .all()
    )
    for entry in sorted(uploads_dir.iterdir()):
        if not entry.is_file() or not STORED_NAME.match(entry.name):
            continue  # 具名真实文档夹具与子目录一律不碰
        if entry.name not in referenced:
            plan.add_file(entry)
    plan.notes.append(f"日志表登记的落盘名 {len(referenced)} 个")
    return plan


def execute(plan: Plan) -> tuple[int, int]:
    """真删。返回 (删除条目数, 释放字节数)"""
    freed = plan_size(plan)
    removed = 0
    for path in plan.files:
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            # 文件被占用（Windows 上 Word 打开着）或权限不足：跳过继续，
            # 回收是尽力而为的运维动作，不该因为一个文件删不掉就整体失败
            continue
    for directory in plan.dirs:
        try:
            if directory.is_dir():
                shutil.rmtree(directory)
                removed += 1
        except OSError:
            continue
    return removed, freed
