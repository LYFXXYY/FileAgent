"""
注册时的默认配置初始化：4 个开箱即用任务 + 通用参考文档（规范库）

【为什么建在注册时】任务与规范库都按 user_id 隔离（§3.1），没有跨用户模板机制；
"项目默认配置"只能落在每个用户自己的空间里。

【两条硬约束】
- **旁路**：初始化失败绝不阻断注册——规范库入库要连 embedding 端点（LM Studio
  不一定在跑），端点没起来时用户照样能注册，规范库可以后续手动上传。
- **幂等**：按名称/标题判重，重复调用不产生第二份；老用户回填与新人注册走同一条路。
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from models.spec_doc import SpecDoc
from models.task import Task
from models.user import User
from services.spec_storage import ingest_spec_doc

logger = logging.getLogger("fileagent.defaults")

# 与前端 Tasks.vue 的 PRESET_TASKS 保持同步；改这里要同时看那边
DEFAULT_TASKS: list[dict] = [
    {
        "name": "作文批改",
        "description": (
            "逐句批改错别字、语法、用词与结构问题，给出修改建议与总评；"
            "下载结果对改动处红字标注，问题清单可点击定位原文。"
        ),
        "prompt_template": (
            "你是一位资深文字编辑，请批改下面的文章。要求：\n"
            "1. 指出错别字与标点错误；\n2. 指出语法和用词不当之处；\n"
            "3. 分析文章结构是否清晰；\n4. 给出总体评价与具体修改建议。\n\n"
            "文档内容：\n{{content}}"
        ),
        "is_grading": True,
        "output_format": "docx",
    },
    {
        "name": "文本润色",
        "description": "在不改变原文意思和语气的前提下，优化措辞与句式，让表达更流畅、更专业。",
        "prompt_template": (
            "请对以下文本进行润色，使表达更流畅、更专业、更自然。"
            "要求保留原文的核心意思、语气和结构，只优化措辞与句式。\n\n原文：\n{{content}}"
        ),
        "is_grading": False,
        "output_format": "docx",
    },
    {
        "name": "PDF 转 PPT",
        "description": (
            "PDF 忠实转换为可编辑 PPT：文字进文本框、表格进原生表、图片原位贴图；"
            "不调用文本大模型，只支持 .pdf 输入（需服务端配置 MINERU_TOKEN）。"
        ),
        "prompt_template": "{{content}}",
        "is_grading": False,
        "output_format": "pptx",
    },
    {
        "name": "文档转 Markdown",
        "description": (
            "把 Word / PDF 转成 Markdown，不调用大模型：Word 本地免费转换；"
            "PDF 走 MinerU 版面识别，保留标题层级、表格与图片引用，带图时打包 zip 下载。"
        ),
        "prompt_template": "{{content}}",
        "is_grading": False,
        "output_format": "md",
    },
]

# 通用参考文档：教学示例稿规范（66 条款四段式，评测夹具同源，见 SPEC §7.7 资产位置）
SPEC_FIXTURE_TITLE = "文字编辑校对规范（教学示例稿）"


def _fixture_path() -> str:
    server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(server_dir, "fixtures", "specs", f"{SPEC_FIXTURE_TITLE}.docx")


def init_user_defaults(db: Session, user: User) -> None:
    """给一个新用户建默认任务与参考文档。整体旁路：任何一步失败只告警，不抛。"""
    try:
        _ensure_default_tasks(db, user)
    except Exception:
        db.rollback()
        logger.exception("默认任务初始化失败 user_id=%s（注册不受影响）", user.id)

    try:
        _ensure_default_spec_doc(db, user)
    except Exception:
        db.rollback()
        logger.warning("默认参考文档入库失败 user_id=%s（可稍后手动上传规范）", user.id)


def _ensure_default_tasks(db: Session, user: User) -> None:
    for spec in DEFAULT_TASKS:
        exists = db.query(Task).filter(Task.user_id == user.id, Task.name == spec["name"]).first()
        if exists is not None:
            continue
        db.add(
            Task(
                user_id=user.id,
                name=spec["name"],
                description=spec["description"],
                prompt_template=spec["prompt_template"],
                is_grading=spec["is_grading"],
                output_format=spec["output_format"],
                # provider_id 留空：走 resolve_provider 的"用户默认 active"回落
            )
        )
    db.commit()


def _ensure_default_spec_doc(db: Session, user: User) -> None:
    exists = (
        db.query(SpecDoc)
        .filter(SpecDoc.user_id == user.id, SpecDoc.title == SPEC_FIXTURE_TITLE)
        .first()
    )
    if exists is not None:
        return
    path = _fixture_path()
    if not os.path.isfile(path):
        logger.warning("默认参考文档夹具缺失：%s", path)
        return
    with open(path, "rb") as f:
        file_bytes = f.read()
    ingest_spec_doc(
        db,
        user_id=user.id,
        title=SPEC_FIXTURE_TITLE,
        filename=os.path.basename(path),
        file_bytes=file_bytes,
    )
    logger.info("默认参考文档已入库 user_id=%s", user.id)
