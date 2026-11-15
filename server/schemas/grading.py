"""
结构化批改的输出契约 —— 问题清单模型、给模型看的 JSON schema、类型归一化

【设计约束】
- 问题类型是**开放输入 + 后端归一化**：批改提示词由用户自定，模型爱用中文类型名，
  硬枚举会在换模型时整片报错。认不出来一律归 other，绝不因为类型名丢掉一条意见。
- 模型只给原文片段，**不给行号也不给字符偏移**（见 services/grading.py 的说明）。
- `strict: True` 的 JSON schema 要求所有字段必填且禁附加属性，所以每个字段都得给
  默认值能填的写法，并且别让 schema 和 GradingIssue 的字段漂移。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# 规范化的问题类型。前端按这个集合出图标和配色，新增要同时改前端
ISSUE_TYPES = (
    "typo",
    "grammar",
    "wording",
    "punctuation",
    "structure",
    "logic",
    "content",
    "other",
)

# 别名表：中英混说都要归到同一类
_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "typo": ("typo", "spelling", "misspelling", "错别字", "错字", "拼写", "字形"),
    "grammar": ("grammar", "syntax", "agreement", "语法", "句法", "搭配", "成分残缺"),
    "wording": ("wording", "vocabulary", "diction", "expression", "用词", "措辞", "词汇", "表达"),
    "punctuation": ("punctuation", "标点", "符号"),
    "structure": (
        "structure",
        "organization",
        "coherence",
        "paragraph",
        "结构",
        "段落",
        "条理",
        "衔接",
    ),
    "logic": (
        "logic",
        "reasoning",
        "argument",
        "consistency",
        "逻辑",
        "论证",
        "矛盾",
        "前后不一致",
    ),
    "content": ("content", "factual", "accuracy", "fact", "内容", "事实", "数据", "准确性"),
}


def _norm(value: str) -> str:
    """比对前的归一化：大小写、下划线、连字符、空格都不该影响类型识别"""
    return value.strip().lower().replace("_", "").replace("-", "").replace(" ", "")


_TYPE_LOOKUP = {
    _norm(alias): canonical for canonical, aliases in _TYPE_ALIASES.items() for alias in aliases
}


def normalize_type(raw: object) -> str:
    """把模型给的类型名归一到 ISSUE_TYPES；认不出来就 other，绝不报错"""
    if not isinstance(raw, str):
        return "other"
    return _TYPE_LOOKUP.get(_norm(raw), "other")


class GradingIssue(BaseModel):
    """一条批改意见。start / end 由后端匹配原文得出，匹配不到就留 None"""

    type: str = "other"
    # 逐字摘自原文的片段，定位全靠它。上限是防模型把整篇文档抄进来
    original: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=1000)
    confidence: float = Field(default=0.5, ge=0, le=1)
    start: int | None = None
    end: int | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: object) -> str:
        return normalize_type(value)

    @field_validator("original", "suggestion", "reason", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        """模型偶尔给 null 或数字，转成字符串而不是让整条意见校验失败"""
        return "" if value is None else str(value)


class GradingResult(BaseModel):
    """
    一次结构化批改的产出

    degraded=True 表示没能结构化（输出不是 JSON 或缺 revised_text），此时 revised_text
    就是模型的原始输出、issues 为空 —— 等价于阶段 0 之前的自由文本行为。
    """

    revised_text: str
    issues: list[GradingIssue] = Field(default_factory=list)
    degraded: bool = False


# 给 chat_json 用的严格 schema。字段名必须和 GradingIssue 一致，改一边要改另一边
GRADING_JSON_SCHEMA: dict = {
    "name": "grading_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "revised_text": {
                "type": "string",
                "description": "批改后的完整正文，保持原文段落数量与顺序，段落间用单个换行分隔",
            },
            "issues": {
                "type": "array",
                "description": "问题清单，没有问题时为空数组",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": list(ISSUE_TYPES)},
                        "original": {
                            "type": "string",
                            "description": "从原文逐字抄出的片段，不含换行，不要改写不要加省略号",
                        },
                        "suggestion": {
                            "type": "string",
                            "description": "修改后的写法，删除类填空串",
                        },
                        "reason": {"type": "string", "description": "一句话说明原因"},
                        "confidence": {"type": "number", "description": "0 到 1"},
                    },
                    "required": ["type", "original", "suggestion", "reason", "confidence"],
                },
            },
        },
        "required": ["revised_text", "issues"],
    },
}
