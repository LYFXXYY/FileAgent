"""
golden set —— 5 份合成中文短文，错误由构造决定，不需要人工标注

【为什么全合成】
真实文档要隐私、要人工标注、每次评测还烧真 token，三样都贵。合成夹具的价值是
"标准答案是构造时的副产品"，改一处 prompt 就能重跑全部，秒级、免费、可进 CI。
代价也摆明：分布不像真实作文，分数会偏乐观，所以真实文档只做人工抽查。

【每个字段的判分口径】
expect_fixes 的每条必须是**能被字符串判定**的替换（错处串 → 正确串），
"表述更清晰"这类没法机械核对的期望一律不写进来——判不了的期望等于没期望。
"""

from __future__ import annotations

from dataclasses import dataclass

_GRADING_INSTRUCTION = (
    "你是一位语文老师。请批改下面的文字：指出错别字、标点误用和语句问题，"
    "逐条给出修改建议，并输出一份修订稿。\n\n文字内容：\n{{content}}"
)


@dataclass(frozen=True)
class GoldenCase:
    """一份夹具 = 一篇文档 + 一套机械可核对的期望"""

    id: str
    name: str
    doc: str
    instruction: str = _GRADING_INSTRUCTION
    # (错处原文, 应该改成) —— 修订落地率按这两串直接判
    expect_fixes: tuple[tuple[str, str], ...] = ()
    # 类型期望按**组**写：组内任一类型命中即算覆盖，组与组之间才要求都有人管。
    # 不用单值是因为"的地得算错别字还是语法"本身没有唯一答案，
    # 让评测台裁决一个语言学争议，结果就是每换一家厂商都要重新解释为什么红了。
    expect_type_groups: tuple[tuple[str, ...], ...] = ()
    min_issues: int = 1
    # 上限只给"干净文档"那种夹具用，用来量误报
    max_issues: int | None = None


CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        id="de-di-de",
        name="的地得混用",
        doc=(
            "他高兴的跳了起来，因为他考的太好了。\n"
            "老师认真的研究了这份作业，并把结果仔细的告诉了大家。\n"
            "这道题做的很顺利，同学们都变的更有信心了。"
        ),
        expect_fixes=(
            ("他高兴的跳了起来", "他高兴地跳了起来"),
            ("考的太好了", "考得太好了"),
            ("认真的研究了", "认真地研究了"),
            ("仔细的告诉了", "仔细地告诉了"),
            ("做的很顺利", "做得很顺利"),
            ("变的更有信心", "变得更有信心"),
        ),
        # 虚词误用三类都算数：模型判成"用词不当"不是漏检
        expect_type_groups=(("typo", "grammar", "wording"),),
        min_issues=4,
    ),
    GoldenCase(
        id="idioms",
        name="成语错别字",
        doc=(
            "会上大家迫不急待地发言，讨论得穿流不息。\n"
            "面对这道难题他一愁莫展，直到老师提示才恍然大悟。\n"
            "对于过去的失误，他抱着即往不咎的心态重新出发了。"
        ),
        expect_fixes=(
            ("迫不急待", "迫不及待"),
            ("穿流不息", "川流不息"),
            ("一愁莫展", "一筹莫展"),
            ("即往不咎", "既往不咎"),
        ),
        expect_type_groups=(("typo",),),
        min_issues=3,
    ),
    GoldenCase(
        id="punctuation",
        name="标点缺失",
        doc=(
            "今天天气很好 我们决定去公园野餐 妈妈准备了三明治和水果。\n"
            "请问这道题应该怎么做 我想了很久也没有头绪。"
        ),
        expect_fixes=(
            ("很好 我们", "很好，我们"),
            ("野餐 妈妈", "野餐，妈妈"),
            ("怎么做 我", "怎么做。我"),
        ),
        expect_type_groups=(("punctuation",),),
        min_issues=2,
    ),
    GoldenCase(
        id="structure",
        name="结构与逻辑",
        doc=(
            "读书很重要。首先我要说运动的好处，运动可以让身体更健康。"
            "其次，读书可以增长知识，开阔视野。最后我们要养成良好的作息习惯，"
            "这样才能保证第二天有精神。总之，读书的意义是不言而喻的。"
        ),
        # 结构类问题没有唯一正确写法，只要求"提出来的意见要能定位"
        expect_fixes=(),
        expect_type_groups=(("structure", "logic"),),
        min_issues=1,
    ),
    GoldenCase(
        id="clean",
        name="干净文档（测误报）",
        doc=(
            "星期天的早晨，我和父亲去菜市场买菜。摊主称好重量，报了价钱，"
            "父亲付了钱便道谢离开。\n回家的路上，他把买来的苹果洗干净，"
            "分给每个人一个。那天下午，我们把周末的计划重新排了一遍。"
        ),
        expect_fixes=(),
        expect_type_groups=(),
        min_issues=0,
        # 一篇通顺的短文被挑出 3 条以上意见，说明模型在硬找问题
        max_issues=2,
    ),
)


def case_by_id(case_id: str) -> GoldenCase:
    """按 id 取夹具，跑单个用例时用"""
    for case in CASES:
        if case.id == case_id:
            return case
    raise KeyError(f"没有 id={case_id} 的夹具，现有：{', '.join(c.id for c in CASES)}")
