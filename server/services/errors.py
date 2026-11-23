"""
面向用户的错误文案与错误编号

【为什么要有这个模块】
`operation_logs.error_message` 是**直接显示在界面上**的字段（Logs 页的错误信息列、
工作台的 el-alert）。之前它混装两种东西：一处写的是给人看的人话（"扫描件落盘记录
缺失，请重新上传"），另一处直接把异常原文甚至 HTTP 响应体塞进去（`内部错误：{e}`、
`body={resp.text[:500]}`）。开放给别人用之后后者就不成立了——那等于把 base_url、
模型名、上游报文甚至框架内部结构送给陌生用户。

【这一层的规矩】
- **界面只看到一句人话 + 编号**。编号（`E-XXX`）是给"用户截图报障、你不在现场"用的：
  念一个号就能定位到抛出点。
- **原始异常文本一个字节都不丢**，但落点是 `logger.exception`（服务端日志）和
  `llm_calls.error_message`（外呼类失败本来就在记），不是界面。
- 抛给上层的异常自带 `code` 时以它为准；认不出类型的走 `E-INTERNAL`，
  并且**必须在日志里留下原文**，否则编号就成了空头支票。

不在这里做的：不改表结构、不加"详情"字段。想深挖就按 log_id 查服务端日志。
"""

from __future__ import annotations

# ---------- LLM 侧 ----------
CODE_LLM_TIMEOUT = "E-LLM-TIMEOUT"
CODE_LLM_NETWORK = "E-LLM-NETWORK"
CODE_LLM_AUTH = "E-LLM-AUTH"
CODE_LLM_RATE = "E-LLM-RATE"
CODE_LLM_SERVER = "E-LLM-SERVER"
CODE_LLM_HTTP = "E-LLM-HTTP"
CODE_LLM_BAD_JSON = "E-LLM-BADJSON"
CODE_LLM_BAD_STRUCT = "E-LLM-BADSTRUCT"
CODE_LLM_CONFIG = "E-LLM-CONFIG"
CODE_LLM_FAILED = "E-LLM-FAILED"
CODE_MODEL_UNAVAILABLE = "E-MODEL-UNAVAILABLE"
CODE_MODEL_CAPABILITY = "E-MODEL-CAPABILITY"

# ---------- 文档与转换侧 ----------
CODE_FILE_MISSING = "E-FILE-MISSING"
CODE_FILE_READ = "E-FILE-READ"
CODE_DOC_EMPTY = "E-DOC-EMPTY"
CODE_TASK_GONE = "E-TASK-GONE"
CODE_DOCX_MD = "E-DOCX-MD"
CODE_PDF_MD = "E-PDF-MD"
CODE_PDF_PPT = "E-PDF-PPT"
CODE_PPT_RENDER = "E-PPT-RENDER"
CODE_PPT_DELIVERY = "E-PPT-DELIVERY"

# ---------- MinerU 云 OCR ----------
CODE_MINERU_UNREACHABLE = "E-MINERU-UNREACHABLE"
CODE_MINERU_FAILED = "E-MINERU-FAILED"
CODE_OCR_EMPTY = "E-OCR-EMPTY"

# ---------- 配额与认证 ----------
CODE_QUOTA_PROCESS = "E-QUOTA-PROCESS"
CODE_QUOTA_REGISTER = "E-QUOTA-REGISTER"
CODE_LOGIN_LOCKED = "E-LOGIN-LOCKED"
# 自助改密时原密码不对——和登录失败同一个语义（凭证校验没过），但要有自己的号，
# 否则运维分不清"有人在爆破登录口"还是"有人在改密口试旧密码"
CODE_PASSWORD_OLD = "E-AUTH-OLDPASSWORD"

# ---------- 服务端配置 ----------
# 密钥加密没配好（API_KEY_ENC_KEY 缺失或与密文不匹配）——这是运维问题，
# 用户与开发者都不该在界面上看到 Python 异常，但必须有一个号能一眼认出来
CODE_SECRET_CONFIG = "E-CONFIG-SECRET"

# ---------- 兜底 ----------
CODE_INTERNAL = "E-INTERNAL"


def tag(message: str, code: str) -> str:
    """
    把编号挂到文案末尾

    只有一处拼接，格式才不会一半有号一半没号。文案本身已经带了编号时不重复追加。
    """
    if message.endswith(f"（{code}）"):
        return message
    return f"{message}（{code}）"


def describe_exception(exc: BaseException) -> tuple[str, str]:
    """
    把一个异常折成 (编号, 给人看的一句话)

    给 catch-all 用的。规则是**宁可说得少，也不说错**：认得出的类型给具体指引，
    认不出的统一一句话 + `E-INTERNAL`，绝不把 `str(exc)` 原样端给用户——
    兜底分支抓到的往往是带 URL、SQL 或框架内部结构的异常文本。

    异常类型在函数内 import：`services/*` 之间横向依赖会把导入顺序绕成环，
    而这张表只在失败路径上跑一次，代价可以忽略。
    """
    # LLMError 自带 code 与人话（构造点已经写好了），直接采信
    from services.llm import LLMError

    if isinstance(exc, LLMError):
        return (exc.code, str(exc))

    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        # 路由层主动抛的 4xx 文案本来就是写给人看的，原样保留
        return (CODE_INTERNAL, str(exc.detail))

    from services.mineru import MinerUError

    if isinstance(exc, MinerUError):
        return (CODE_MINERU_FAILED, "云端识别失败，请稍后重试；若持续失败请联系维护者")

    from services.pdf import PdfError, ScannedPdfError

    if isinstance(exc, ScannedPdfError):
        return (CODE_OCR_EMPTY, "这份 PDF 没有可识别的文字层，请改用扫描件处理或检查清晰度")
    if isinstance(exc, PdfError):
        return (CODE_DOC_EMPTY, "无法从这份文档取到正文，可能是加密或已损坏")

    from services.ppt_convert import PptConvertError

    if isinstance(exc, PptConvertError):
        return (CODE_PPT_RENDER, "版面拼装失败，请重试；若持续失败请联系维护者")

    from services.spec_storage import SpecStorageError

    if isinstance(exc, SpecStorageError):
        return (CODE_DOC_EMPTY, "这份文档没能入库，请检查内容是否为可提取的文字")

    if isinstance(exc, OSError):
        # 磁盘/权限类：说"服务器读不到文件"比抛出 [Errno 13] 有用
        return (CODE_FILE_READ, "服务器读取文件时出错，请重新上传该文件再试")

    return (
        CODE_INTERNAL,
        "处理时遇到未预期的问题，详细信息已记录。请稍后重试；若反复出现，"
        "请把这段说明和上面的编号反馈给维护者",
    )
