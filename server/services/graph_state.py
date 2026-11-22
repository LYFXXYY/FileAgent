r"""
分块批改的图状态回收：`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `store`

【和 storage.py 的分工】那边管**磁盘产物**，这里管**库里的行**。只有批改类长文档会写
这四张表（非批改任务不建图），单份几十 KB 看着不急，但它**只增不减**：每跑一次长文
分块就留下一批线程行，e2e 更是跑一轮攒一轮。

【线程标识】
- checkpoint 三表的 `thread_id` 形如 `log-{operation_logs.id}`（`api/v1/process.py` 传的）；
- 按块缓存在 `store` 表，`prefix` 形如 `longdoc.log-{id}`、`key` 是块序号
  （`graphs/grading_graph.py` 的 `namespace = ("longdoc", thread_id)`）。
- **只认这两种形态**，其他 thread_id 一概不动——将来若引入别的图，不会被这里误删。

【四条必须守住的规矩】
1. **非终态日志的线程绝不删**。`pending` / `processing` 正靠 checkpoint 续跑、靠按块缓存
   省钱（AGENTS §3.10："省钱的根据是按块 store，不是 checkpoint"）。
2. **`failed` 也照样要留到过期**。用户点"重试"时命中的就是那份按块缓存；把它删了，
   重试就从"只补失败的块"退化成"整篇重打一遍"，正是要避免的双倍烧 token。
   所以可回收 = 终态（done/failed）**且**距最后一次状态变更超过留存期。
3. **四张表一起删**。实测它们之间**没有外键**（`pg_constraint` 查这四张表零行），
   漏一张就是永久垃圾，而且 `checkpoints.parent_checkpoint_id` 自引用会留下指向
   不存在行的 blob。
4. **时间基准走 DB 时钟**（`database.db_now()`，AGENTS §7.3），比较的列用
   `coalesce(processed_at, uploaded_at)`——`processed_at` 每次状态变更都刷新，
   正是"最后一次被用到"的近似；历史行可能为 NULL，退回上传时间。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from database import db_now
from models.operation_log import LogStatus, OperationLog

# 只回收这两个形态，其余线程不属于"某条日志的分块批改状态"
THREAD_RE = re.compile(r"^log-(\d+)$")
STORE_PREFIX_RE = re.compile(r"^longdoc\.log-(\d+)$")

# 终态：处理已经结束，图状态最多给"重试"当缓存用
TERMINAL = (LogStatus.DONE, LogStatus.FAILED)

_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@dataclass
class GraphStatePlan:
    """一批要回收的线程号，过期与孤儿分开列，看清单时能判断每一条为什么可以删"""

    expired: list[int] = field(default_factory=list)
    orphans: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def log_ids(self) -> list[int]:
        return sorted(set(self.expired) | set(self.orphans))

    def count(self) -> int:
        return len(self.log_ids())


def _thread_log_ids(threads: set[str], prefixes: set[str]) -> set[int]:
    """把 `log-7` / `longdoc.log-7` 归并成日志 id 集合，形态不对的一律忽略"""
    ids = {int(m.group(1)) for m in (THREAD_RE.match(t) for t in threads) if m}
    ids |= {int(m.group(1)) for m in (STORE_PREFIX_RE.match(p) for p in prefixes) if m}
    return ids


def discover_graph_state(db: Session) -> tuple[set[str], set[str]]:
    """
    从四张表里把现存线程捞出来（Postgres 侧的原始 SQL，不经 ORM）

    这四张表不在我们的 metadata 里，由 LangGraph 的 `setup()` 自动建，所以只能按表名查。
    逐表 UNION 而不是一次 JOIN：任一表缺失（还没跑过长文批改）时整条 SQL 会报错，
    而"没有图状态"是全新环境的正常状态，不该让体检脚本崩。
    """
    threads: set[str] = set()
    for table in _TABLES:
        try:
            rows = db.execute(text(f"SELECT DISTINCT thread_id FROM {table}")).scalars()
        except Exception:
            db.rollback()
            continue
        threads.update(r for r in rows if r)

    prefixes: set[str] = set()
    try:
        rows = db.execute(text("SELECT DISTINCT prefix FROM store")).scalars()
    except Exception:
        db.rollback()
    else:
        prefixes.update(r for r in rows if r)
    return threads, prefixes


def plan_graph_state(
    db: Session,
    days: int,
    *,
    threads: set[str] | None = None,
    prefixes: set[str] | None = None,
) -> GraphStatePlan:
    """
    分出"过期可回收"和"孤儿可回收"两类线程号

    `threads` / `prefixes` 可注入，是为了让分类逻辑能在内存 SQLite 上单测——
    四张 LangGraph 表不在测试库里，但"哪些 id 可以删"这件事与它们怎么被发现无关。
    """
    plan = GraphStatePlan()
    if threads is None or prefixes is None:
        found_threads, found_prefixes = discover_graph_state(db)
        threads = found_threads if threads is None else threads
        prefixes = found_prefixes if prefixes is None else prefixes

    ids = _thread_log_ids(threads or set(), prefixes or set())
    if not ids:
        plan.notes.append("库里没有分块批改的图状态")
        return plan

    last_used = func.coalesce(OperationLog.processed_at, OperationLog.uploaded_at)
    rows = db.execute(
        select(OperationLog.id, OperationLog.status, last_used).where(OperationLog.id.in_(ids))
    ).all()
    present = {r[0]: (r[1], r[2]) for r in rows}

    # 孤儿：日志行已经没了（e2e 删库或将来引入日志删除），图状态指向不存在的对象
    plan.orphans = sorted(ids - set(present))

    if days <= 0:
        plan.notes.append("retention_days=0，跳过过期回收（孤儿仍然可删）")
        return plan

    cutoff = db_now(db) - timedelta(days=days)
    stale: list[int] = []
    for log_id, (status, used_at) in present.items():
        if status not in TERMINAL or used_at is None:
            continue  # 规矩 1：非终态的一律保留
        if used_at < cutoff:
            stale.append(log_id)
    plan.expired = sorted(stale)
    plan.notes.append(f"图状态线程 {len(ids)} 个，终态且早于 {cutoff:%Y-%m-%d} 的 {len(stale)} 个")
    return plan


def graph_state_sizes(db: Session) -> dict[str, int]:
    """各表占用的字节数（含索引）。Postgres 专有，非 PG 环境返回空字典"""
    sizes: dict[str, int] = {}
    for table in (*_TABLES, "store"):
        try:
            value = db.execute(text("SELECT pg_total_relation_size(:t)"), {"t": table}).scalar()
        except Exception:
            db.rollback()
            return {}
        sizes[table] = int(value or 0)
    return sizes


def purge_graph_state(db: Session, log_ids: list[int]) -> dict[str, int]:
    """
    按日志 id 删四张表里的对应行，返回每张表删掉的行数

    一次 commit：要么整批线程删干净，要么都不删——只删一半正是"留下指向不存在行的
    blob"那种脏状态的来源。
    """
    deleted: dict[str, int] = {}
    if not log_ids:
        return deleted

    threads = [f"log-{i}" for i in log_ids]
    prefixes = [f"longdoc.log-{i}" for i in log_ids]
    for table in _TABLES:
        result = db.execute(
            text(f"DELETE FROM {table} WHERE thread_id = ANY(:tids)"),
            {"tids": threads},
        )
        deleted[table] = int(result.rowcount or 0)
    result = db.execute(
        text("DELETE FROM store WHERE prefix = ANY(:prefixes)"), {"prefixes": prefixes}
    )
    deleted["store"] = int(result.rowcount or 0)
    db.commit()
    return deleted


def vacuum_graph_state(engine) -> dict[str, bool]:
    """
    对四张表跑 `VACUUM (ANALYZE)`，让回收的空间可被复用

    DELETE 之后**表文件不会缩小**（Postgres 只把页标成可复用），所以"回收了 0.9 MB"
    与"磁盘少了 0.9 MB"是两件事。普通 VACUUM 不排他、不阻塞业务，够本工具用；
    要把文件真缩回磁盘得 `VACUUM FULL`（拿排他锁），运维脚本不该顺手要锁。

    VACUUM 不能在事务块里跑，所以单开一条 AUTOCOMMIT 连接，不复用调用方的 Session。
    """
    done: dict[str, bool] = {}
    with engine.connect() as raw:
        conn = raw.execution_options(isolation_level="AUTOCOMMIT")
        for table in (*_TABLES, "store"):
            try:
                conn.execute(text(f"VACUUM (ANALYZE) {table}"))
                done[table] = True
            except Exception:
                done[table] = False
    return done
