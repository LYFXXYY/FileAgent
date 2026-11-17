"""
视觉批评意见的契约（阶段 5 s5.5/s5.6）

【为什么单独成模块】
- 批评循环的 VLM 输出和批改清单是两种 schema——硬塞 schemas/grading.py 会让两边都别扭。
- s5.6 的条件边判定（"通过 / 还要改"）需要稳定的 verdict 字段，提前定义能避免后面拆字段。

【verdict 取值】
- "pass"：批评认为当前稿已合格，循环退出
- "needs_revision"：有问题要修，issues 非空
- 模型不守格式 / 解析失败 → parse 退到 "needs_revision" + 单条占位 issue，
  degraded=True。绝不返回 "pass"——把"没听清"当成"通过了"会让坏稿直接交付。

【image_refs 字段约束（§9.12.2.C）】
s5.5 不真发图给 VLM；这条留给 s5.4 渲染接通后填。critique prompt 里以 base64 data URL
形式带图，s5.6 的 critic 节点负责拼装。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

VALID_VERDICTS = ("pass", "needs_revision")


class CritiqueIssue(BaseModel):
    """单条视觉批评意见"""

    slide_index: int = Field(ge=1, description="第几页，1 起")
    category: str = "other"
    severity: str = "medium"
    description: str
    suggestion: str = ""


class Critique(BaseModel):
    """一次 VLM 批评的结构化结果"""

    verdict: str = "needs_revision"
    issues: list[CritiqueIssue] = Field(default_factory=list)
    degraded: bool = False

    @field_validator("verdict")
    @classmethod
    def _verdict_in_set(cls, v: str) -> str:
        if v not in VALID_VERDICTS:
            return "needs_revision"
        return v


VLM_CRITIQUE_JSON_SCHEMA: dict = {
    "name": "ppt_visual_critique",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(VALID_VERDICTS),
                "description": "pass 表示无需修订可直接交付；needs_revision 表示有下方任一问题",
            },
            "issues": {
                "type": "array",
                "description": "具体批评点列表。verdict=pass 时为空数组",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "slide_index": {
                            "type": "integer",
                            "description": "第几页（1 起）",
                        },
                        "category": {
                            "type": "string",
                            "description": (
                                "问题大类：layout/text_overflow/title_overlap/contrast/bullet_density/other"
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "description": {
                            "type": "string",
                            "description": "一句话说明问题",
                        },
                        "suggestion": {
                            "type": "string",
                            "description": "怎么改",
                        },
                    },
                    "required": ["slide_index", "category", "severity", "description"],
                },
            },
        },
        "required": ["verdict", "issues"],
    },
}


def parse_critique(raw_text: str, parsed_json: dict[str, Any] | None) -> Critique:
    """
    把 VLM 输出转成 Critique。任何不合形都退到 needs_revision + degraded=True

    阶梯：
    1. parsed_json 形状正确 → 正常解析
    2. parsed_json 缺 verdict / issues 不是 list → 用 raw_text 当 description 占位
    3. raw_text 是空字符串 → 同样退到占位（不能假装通过）
    """
    if parsed_json is not None and isinstance(parsed_json.get("issues"), list):
        try:
            issues = [CritiqueIssue(**i) for i in parsed_json["issues"]]
            verdict = parsed_json.get("verdict", "needs_revision")
            return Critique(verdict=verdict, issues=issues, degraded=False)
        except Exception:
            pass

    # 阶梯 2/3：硬塞一条"看不懂模型说啥"的占位，verdict 强制 needs_revision
    fallback = CritiqueIssue(
        slide_index=1,
        category="other",
        severity="low",
        description=f"模型未按批评契约输出，原始回复前 200 字：{raw_text[:200]!r}"
        if raw_text
        else "模型未按批评契约输出（空回复）",
        suggestion="检查模型是否守 response_format 或换提供商",
    )
    return Critique(verdict="needs_revision", issues=[fallback], degraded=True)


def serialize_critique(critique: Critique) -> str:
    return json.dumps(critique.model_dump(), ensure_ascii=False)
