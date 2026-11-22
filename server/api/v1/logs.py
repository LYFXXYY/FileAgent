"""
操作日志查询路由 —— 分页 + 只看自己的

【分页设计】
- page 从 1 开始，page_size 默认 20，最大 100
- 类比 Java PageRequest.of(page - 1, size)

【筛选设计】
- status：按处理状态筛选（pending/processing/done/failed），非法值直接 400
- filename：按文件名模糊搜索（ilike，不区分大小写）
- 两者可组合使用，也可都不传（查全部）

【查询投影】
两条查询都 defer 掉 original_content / processed_content 两份正文大列：响应模型本来
就不含它们，但 SELECT * 会把整页 20 行的全文拉进内存，长文档时每页几百 KB 白跑一趟。
structured_output 留着不 defer，因为条数要靠它算，而"怎么算条数"只该有一处定义
（services/grading.deserialize_issues）——在 SQL 里再写一遍 json 解析就是第二处。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, defer

from database import get_db
from middleware.auth import get_current_user
from models.operation_log import LogStatus, OperationLog
from models.user import User
from schemas.operation_log import LogListResponse, LogResponse
from services.grading import deserialize_issues

router = APIRouter()

# 不给正文大列进 SELECT 的共用投影选项
_LIGHT_COLUMNS = (defer(OperationLog.original_content), defer(OperationLog.processed_content))


def _to_response(log: OperationLog) -> LogResponse:
    """
    ORM 字段之外补一个 issue_count

    列表页只取条数、不取清单本身；正文两列已在查询层 defer 掉，这里碰不到它们。
    历史数据和非批改任务都是 NULL，解析结果自然为 0。
    """
    count = len(deserialize_issues(log.structured_output))
    return LogResponse.model_validate(log).model_copy(update={"issue_count": count})


@router.get("", response_model=LogListResponse)
def list_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(
        default=None, description="按状态筛选：pending/processing/done/failed"
    ),
    filename: str | None = Query(default=None, description="按文件名模糊搜索"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """分页查询当前用户的操作日志（时间倒序），支持状态筛选和文件名搜索"""
    q = db.query(OperationLog).options(*_LIGHT_COLUMNS).filter(OperationLog.user_id == current.id)

    # 状态筛选：非法值必须报错，不能静默当"不过滤"——
    # 用户以为筛了 failed 却拿到全部记录，比报错更坏
    if status:
        try:
            status_value = LogStatus(status)
        except ValueError:
            allowed = " / ".join(item.value for item in LogStatus)
            raise HTTPException(400, f"无效的状态筛选值：{status}（可选 {allowed}）") from None
        q = q.filter(OperationLog.status == status_value)

    # 文件名模糊搜索（ilike 不区分大小写）
    if filename:
        q = q.filter(OperationLog.original_filename.ilike(f"%{filename}%"))

    total = q.count()
    items = (
        q.order_by(OperationLog.uploaded_at.desc(), OperationLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return LogListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[_to_response(i) for i in items],
    )


@router.get("/{log_id}", response_model=LogResponse)
def get_log(
    log_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """查看单条日志（详情）"""
    log = (
        db.query(OperationLog)
        .options(*_LIGHT_COLUMNS)
        .filter(OperationLog.id == log_id, OperationLog.user_id == current.id)
        .first()
    )
    if log is None:
        raise HTTPException(404, "记录不存在")
    return _to_response(log)
