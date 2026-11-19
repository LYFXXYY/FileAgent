"""
结构化批改服务 —— 输出契约的拼装、解析、以及由后端做的原文定位

【为什么坐标不让模型给】
LLM 不数字符。让它输出 paragraph_index 或 char_offset 看着整齐，实际会大面积错位，
而且错了也没有任何信号，批注指到错地方比不指更糟。所以只让它逐字摘出原文片段，
偏移量由后端在 original_content 上精确匹配得出；匹配不上就留空，不猜。

【为什么解析失败要让任务继续成功】
模型不按格式出货是常态而不是异常，尤其批改提示词由用户自写、五花八门。这时退回旧的
自由文本行为（整段输出当修订结果、问题清单为空），比把用户文档判成失败有用得多。
差异只体现在 issues 为空 + degraded 标记上，下载和 diff 完全不受影响。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from schemas.grading import GradingIssue, GradingResult

logger = logging.getLogger("fileagent.grading")

# 批改契约走 system，用户自己的批改要求走 user：两者混在一条消息里时，
# 模型会把格式约束当内容一起改写，出格率明显上升
GRADING_OUTPUT_INSTRUCTION = """你是一名严谨的文档批改助手。

只输出一个 JSON 对象，不要输出任何解释文字、前后缀，也不要包 Markdown 代码块围栏。字段约定：

- revised_text：批改后的完整正文。保持原文的段落数量与顺序，段落之间用单个换行分隔；
  不需要改动的段落原样保留，不要合并或拆分段落。
- issues：问题清单数组，没有问题时给空数组。每项含五个字段：
  - type：从 typo / grammar / wording / punctuation / structure / logic / content / other 中选一个
  - original：从原文逐字抄出的片段，10 到 60 个字为宜，不含换行，不要改写、不要加省略号。
    位置由系统自己匹配，你不要也不要输出任何行号或字符偏移。
  - suggestion：修改后的写法；纯删除类意见填空字符串
  - reason：一句话说明为什么，直接给结论，不要客套
  - confidence：0 到 1 的小数。把握低于 0.5 的不要写进 issues

同一处文字只保留一条意见；拿不准的宁可不报。"""


# 召回条款的包装：位置在用户文档之后（模型对 prompt 尾部更敏感），
# 并且明确"不适用就不要硬套"——否则模型会给每个问题都挂一条不相干的条款号。
SPEC_CONTEXT_TEMPLATE = """以下是从规范库里按相关度召回的条款，判断时优先依据它们：

{context}

要求：
- 只在与本文确实相关时依据这些条款，并在 reason 末尾用（依据 条款号）标注；不适用的条款不要引用。
- 召回不等于本文一定有对应错误，不要因为"规范里有这条"就报问题。"""


def build_messages(prompt: str, spec_context: str = "") -> list[dict[str, str]]:
    """
    拼批改请求：契约在 system，用户的批改要求（+ 可选的规范条款）在 user

    条款上下文进 user 而不是 system：它是**内容**（随每次检索变化），不是契约。
    混进 system 会让模型把格式约束当材料一起改写，出格率明显上升。
    """
    user = prompt
    if spec_context:
        user = f"{prompt}\n\n{SPEC_CONTEXT_TEMPLATE.format(context=spec_context)}"
    return [
        {"role": "system", "content": GRADING_OUTPUT_INSTRUCTION},
        {"role": "user", "content": user},
    ]


def locate_issues(
    issues: list[GradingIssue],
    original_text: str,
) -> list[GradingIssue]:
    """
    给每条意见在原文里找一个尚未被占用的位置

    同一个 original 在原文里出现多次、且模型提了多条时，依次认领不同的出现位置，
    避免所有同名批注全指到第一处。匹配不到就保持 start=None，前端据此显示"未定位"。
    """
    claimed: list[tuple[int, int]] = []
    located: list[GradingIssue] = []

    for issue in issues:
        span = _find_unused_span(issue.original, original_text, claimed)
        if span is None:
            located.append(issue)
            continue
        claimed.append(span)
        located.append(issue.model_copy(update={"start": span[0], "end": span[1]}))

    return located


def _find_unused_span(
    fragment: str,
    text: str,
    claimed: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """从左往右找第一个不与已认领区间重叠的出现位置"""
    if not fragment or not text:
        return None

    search_from = 0
    while True:
        start = text.find(fragment, search_from)
        if start == -1:
            return None
        end = start + len(fragment)
        overlaps = any(
            start < claimed_end and claimed_start < end for claimed_start, claimed_end in claimed
        )
        if not overlaps:
            return (start, end)
        # 一定要前进，否则同一个起点会被反复命中而死循环
        search_from = start + 1


def parse_grading_output(
    raw_content: str,
    payload: Any,
    original_text: str,
) -> GradingResult:
    """
    把模型响应整理成 GradingResult

    payload 是 chat_json 的解析产物，None 表示端点压根没吐 JSON。任何不合用的形态
    都降级成自由文本，不抛异常 —— 用户的文档必须能拿到一个结果。
    """
    if isinstance(payload, dict):
        revised = payload.get("revised_text")
        if isinstance(revised, str) and revised.strip():
            return GradingResult(
                revised_text=revised,
                issues=_parse_issues(payload.get("issues"), original_text),
            )
        # 缺 revised_text 就没有修订文可做 diff，整条降级而不是去猜一份
        logger.warning("批改 JSON 缺少可用的 revised_text，降级为自由文本输出")
    else:
        logger.warning("批改输出不是 JSON，降级为自由文本输出")

    return GradingResult(revised_text=raw_content, issues=[], degraded=True)


def _parse_issues(raw_issues: object, original_text: str) -> list[GradingIssue]:
    """逐条校验：单条不合格只丢那一条，不连累整份清单"""
    if not isinstance(raw_issues, list):
        return []

    parsed: list[GradingIssue] = []
    for item in raw_issues:
        try:
            parsed.append(GradingIssue.model_validate(item))
        except ValidationError as e:
            logger.warning("丢弃一条不合规的批改意见：%s", e.errors()[0].get("msg", e))
            continue

    return locate_issues(parsed, original_text)


def serialize_result(
    result: GradingResult,
    *,
    refs: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> str:
    """
    落库用：operation_logs.structured_output 存这一个 JSON 串

    `refs` 是本次真正进过 prompt 的规范条款（阶段 6）。记在信封里而不是让用户去猜：
    "有没有用规范"和"用了哪几条"必须能从结果本身查出来，否则 RAG 出没生效看不出来。
    `note` 是检索被跳过/失败的原因，和 refs 空列表区分开——"库里没相关内容"
    与"编码端点没起来"是两件事，混在一起显示就等于撒谎（同 §3.7 的 NULL vs []）。
    """
    payload: dict[str, Any] = {
        "degraded": result.degraded,
        "issues": [i.model_dump() for i in result.issues],
    }
    if refs:
        payload["refs"] = refs
    if note:
        payload["spec_note"] = note
    return json.dumps(payload, ensure_ascii=False)


def deserialize_refs(stored: str | None) -> list[dict[str, Any]]:
    """本次批改引用了哪些规范条款。没有（含历史数据）就是空列表"""
    refs = _deserialize(stored).get("refs")
    return refs if isinstance(refs, list) else []


def spec_note(stored: str | None) -> str | None:
    """规范检索的降级说明。正常引用了条款或压根没开规范库时都是 None"""
    note = _deserialize(stored).get("spec_note")
    return note if isinstance(note, str) and note else None


def deserialize_issues(stored: str | None) -> list[dict[str, Any]]:
    """
    出库给前端用：解析 structured_output 里的问题清单，坏数据返回空而不是 500

    库里存的是历史 JSON 串，手改过或版本变更都可能让它不合形；这是只读展示路径，
    解析不了就当没有问题清单。
    """
    return _deserialize(stored).get("issues", [])


def is_degraded(stored: str | None) -> bool:
    """
    这条批改有没有从结构化退回自由文本

    前端必须能区分"批了但没发现问题"和"没能结构化所以没清单项"。
    少了这个，两者都显示 0 条问题，属于撒谎。
    """
    return bool(_deserialize(stored).get("degraded"))


def _deserialize(stored: str | None) -> dict[str, Any]:
    """解析 structured_output 的 JSON 信封。不合形一律给空信封"""
    if not stored:
        return {}
    try:
        payload = json.loads(stored)
    except (json.JSONDecodeError, TypeError):
        logger.warning("structured_output 解析失败，按无问题清单处理")
        return {}
    return payload if isinstance(payload, dict) else {}
