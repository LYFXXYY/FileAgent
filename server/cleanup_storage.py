"""一次性/定期回收：磁盘产物 + 分块批改的库内图状态。

跑法（在 server/ 下）：
    uv run python cleanup_storage.py                  # 只看会删什么，不删（默认档）
    uv run python cleanup_storage.py --days 7         # 临时改留存期看效果（仍不删）
    uv run python cleanup_storage.py --apply          # 真删
    uv run python cleanup_storage.py --apply --vacuum # 删完再对图状态表跑 VACUUM (ANALYZE)

为什么默认是"只看"：这是一次会删文件与删库行动作，让人先扫一眼清单比"跑完才发现删多了"便宜。

它解决三件事，别混为一谈：
- **过期产物**：终态且超过 `retention_days` 的日志，其 pptx 二进制 / MinerU 产物 / 输入原件；
  注意批改与普通 docx **不占磁盘**（下载时从库里现算），所以与此数无关；
- **孤儿上传文件**：`uploads/` 里没有任何日志指向的落盘文件（历史 e2e 攒下来的那批）；
- **图状态**：长文分块批改写进 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` /
  `store` 的线程行。同样按留存期收，但判据是"最后一次状态变更"，且 **failed 的线程在窗口内
  必须留着**——那是重试时省钱（不重打已成功的块）的唯一依据，见 services/graph_state.py。

安全边界见 services/storage.py 与 services/graph_state.py 顶部：只删 DB 指向得上的、
只认 uuid 落盘名与 `log-<数字>` 线程形态，用户手工放进 uploads/ 的具名真实文档夹具
永不匹配、永不被删。
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy.orm import sessionmaker

import database
from config import get_settings
from services.graph_state import (
    GraphStatePlan,
    graph_state_sizes,
    plan_graph_state,
    purge_graph_state,
    vacuum_graph_state,
)
from services.storage import Plan, execute, plan_expired, plan_orphans, plan_size, resolve_dir


def _report(label: str, plan: Plan, limit: int = 15) -> None:
    print(f"\n[{label}] 可回收 {plan.count()} 项 / {plan_size(plan) / 1024 / 1024:.1f} MB")
    for note in plan.notes:
        print(f"    · {note}")
    for path in (plan.files + plan.dirs)[:limit]:
        print(f"    - {path.name}")
    if plan.count() > limit:
        print(f"    … 另 {plan.count() - limit} 项")


def _report_graph(db, plan: GraphStatePlan) -> None:
    expired, orphan = len(plan.expired), len(plan.orphans)
    print(f"\n[图状态] 可回收线程 {plan.count()} 个（过期 {expired} / 孤儿 {orphan}）")
    for note in plan.notes:
        print(f"    · {note}")
    sizes = graph_state_sizes(db)
    if sizes:
        total = sum(sizes.values())
        detail = "，".join(f"{t} {sizes[t] / 1024:.0f} kB" for t in sorted(sizes))
        print(f"    · 四张表现占 {total / 1024 / 1024:.1f} MB：{detail}")
    for log_id in plan.log_ids()[:15]:
        print(f"    - log-{log_id}")
    if plan.count() > 15:
        print(f"    … 另 {plan.count() - 15} 个线程")


def run(days: int, apply_changes: bool, vacuum: bool = False) -> int:
    settings = get_settings()
    uploads_dir = Path(resolve_dir(settings.upload_dir))
    outputs_dir = Path(resolve_dir(settings.output_dir))
    print(f"uploads={uploads_dir}\noutputs={outputs_dir}\nretention_days={days}")

    db = sessionmaker(bind=database.engine)()
    try:
        expired = plan_expired(db, uploads_dir, outputs_dir, days)
        orphans = plan_orphans(uploads_dir, db)
        graph = plan_graph_state(db, days)
        _report("过期产物", expired)
        _report("孤儿上传文件", orphans)
        _report_graph(db, graph)

        combined = Plan(files=expired.files + orphans.files, dirs=expired.dirs + orphans.dirs)
        if combined.count() == 0 and graph.count() == 0:
            print("\n没有可回收的东西。")
            return 0
        if not apply_changes:
            print(
                f"\n[check] 未删除任何东西：文件 {combined.count()} 项 / "
                f"{plan_size(combined) / 1024 / 1024:.1f} MB，图状态线程 {graph.count()} 个。"
                "确认清单后加 --apply 执行"
            )
            if vacuum:
                print("  --vacuum 只在 --apply 那一趟才跑，检查档什么都没做")
            return 0

        removed, freed = execute(combined)
        print(
            f"\n[done] 已删除文件 {removed}/{combined.count()} 项，"
            f"释放 {freed / 1024 / 1024:.1f} MB"
        )
        if removed < combined.count():
            print("  有条目没删掉（Windows 上文件被 Word 等资源管理器占用），关掉后重跑即可")
        if graph.count():
            rows = purge_graph_state(db, graph.log_ids())
            detail = "，".join(f"{t}={n}" for t, n in rows.items())
            print(f"[done] 已回收图状态线程 {graph.count()} 个：{detail}")
        if vacuum:
            result = vacuum_graph_state(database.engine)
            if all(result.values()):
                print("[done] 图状态表已 VACUUM (ANALYZE)，空间供后续复用")
            else:
                bad = "，".join(t for t, ok in result.items() if not ok)
                print(f"[warn] VACUUM 未完成的表：{bad}（数据已删干净，只是空间没回收）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="回收 uploads/outputs 磁盘占用与分块批改的图状态")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只列清单）")
    parser.add_argument("--days", type=int, default=None, help="覆盖 RETENTION_DAYS")
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="收尾时对图状态表跑 VACUUM (ANALYZE)，与 --apply 搭配用",
    )
    args = parser.parse_args()
    days = args.days if args.days is not None else get_settings().retention_days
    sys.exit(run(days=days, apply_changes=args.apply, vacuum=args.vacuum))
