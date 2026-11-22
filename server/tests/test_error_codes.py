"""
错误编号层（services/errors.py）

钉的是两条不变式：
1. 每条失败都带编号，且不会重复追加；
2. **兜底分支不得把异常原文端给用户**——那正是开放给别人用之前最需要堵的口子
   （`内部错误：{e}` 会带出框架内部结构、URL、甚至上游报文片段）。
"""

import pytest

from services.errors import (
    CODE_INTERNAL,
    CODE_LLM_AUTH,
    CODE_MINERU_FAILED,
    CODE_OCR_EMPTY,
    describe_exception,
    tag,
)
from services.llm import LLMError
from services.mineru import MinerUError


def test_tag_appends_once():
    once = tag("模型服务限流，请稍后重试", "E-LLM-RATE")
    assert once.endswith("（E-LLM-RATE）")
    # 调用点自己带了号就不再追加，否则会看到「…（E-X）（E-X）」
    assert tag(once, "E-LLM-RATE") == once


def test_llm_error_code_is_adopted():
    exc = LLMError("模型密钥无效", code=CODE_LLM_AUTH, detail="LLM 返回 401：secret-leak")
    code, human = describe_exception(exc)
    assert code == CODE_LLM_AUTH
    assert human == "模型密钥无效"
    # 原文仍在异常对象上，由调用方写进日志与计量表
    assert "secret-leak" in exc.detail


def test_mineru_error_gets_code_and_generic_text():
    code, human = describe_exception(MinerUError("任务 123 失败：queue full"))
    assert code == CODE_MINERU_FAILED
    assert "queue full" not in human


def test_oserror_is_reported_as_a_readable_action():
    code, human = describe_exception(PermissionError(13, "Permission denied"))
    assert code == "E-FILE-READ"
    assert "Permission denied" not in human
    assert "重新上传" in human


def test_unknown_exception_falls_back_without_leaking():
    """
    兜底分支是整个改动的核心：认不出类型时给统一一句话 + E-INTERNAL，
    异常文本一个字都不出现在用户可见的文案里。
    """
    weird = RuntimeError("SELECT * FROM operation_logs WHERE user_id=17 failed at 0x7f9")

    code, human = describe_exception(weird)

    assert code == CODE_INTERNAL
    assert "SELECT" not in human
    assert "0x7f9" not in human
    assert "operation_logs" not in human


def test_scanned_and_empty_pdfs_keep_their_own_codes():
    from services.pdf import ScannedPdfError

    code, human = describe_exception(ScannedPdfError("没有文字层"))
    assert code == CODE_OCR_EMPTY
    assert human


@pytest.mark.parametrize(
    "code",
    ["E-LLM-TIMEOUT", "E-MINERU-UNREACHABLE", "E-QUOTA-PROCESS", "E-LOGIN-LOCKED"],
)
def test_code_names_are_stable_strings(code):
    """
    编号是**对外契约**（用户截图念的就是这一串），改名等于让历史报障失效。
    这里用字面量断言，防止有人顺手重构掉。
    """
    from services import errors

    assert code in vars(errors).values()
