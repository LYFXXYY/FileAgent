"""任务 schema 校验测试 —— output_format 列（阶段 5 s5.1）

纯 Pydantic 校验，不连库。TaskCreate 的合法性、TaskResponse 的 roundtrip 都覆盖。
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from schemas.task import TaskCreate, TaskResponse


def _minimal_payload(**overrides):
    base = {
        "name": "t",
        "prompt_template": "请润色：\n{{content}}",
        "is_grading": False,
        "provider_id": None,
    }
    base.update(overrides)
    return base


def test_output_format_defaults_to_docx():
    """不传 output_format 时回退 docx，保证阶段 0–4 期间的老 API 调用方不破坏"""
    body = TaskCreate(**_minimal_payload())
    assert body.output_format == "docx"


def test_output_format_accepts_pptx():
    body = TaskCreate(**_minimal_payload(output_format="pptx"))
    assert body.output_format == "pptx"


def test_output_format_rejects_unknown():
    with pytest.raises(ValidationError, match="output_format"):
        TaskCreate(**_minimal_payload(output_format="xlsx"))


def test_output_format_rejects_uppercase():
    """取值大小写敏感——避免 'DOCX' 在路由派发时悄悄走到默认分支"""
    with pytest.raises(ValidationError):
        TaskCreate(**_minimal_payload(output_format="DOCX"))


def test_task_response_roundtrips_output_format():
    resp = TaskResponse(
        id=1,
        user_id=1,
        provider_id=None,
        name="t",
        description=None,
        prompt_template="x",
        is_grading=False,
        output_format="pptx",
        created_at=datetime(2026, 11, 16),
        updated_at=datetime(2026, 11, 16),
    )
    assert resp.output_format == "pptx"
