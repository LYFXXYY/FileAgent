"""
PPT 大纲生成（阶段 5 s5.2）的提示词构造层

与 services/grading.py 同形：契约放 system，用户的处理要求在 user。
"""

from __future__ import annotations

OUTLINE_OUTPUT_INSTRUCTION = """\
你的任务：把用户给的文档原文提炼成一份**幻灯片大纲**，输出严格 JSON。

【输出契约】
{
  "title": "整篇 PPT 的总标题",
  "slides": [
    {
      "title": "本页标题（不超过 30 字）",
      "bullets": ["要点 1（每条不超过 60 字）", "要点 2", ...],
      "image_placeholders": []
    },
    ...
  ]
}

【硬性规则】
1. slides 至少 1 页；正文长时按段落逻辑分页，**不要把所有内容塞进 1 页**。
2. bullets 是给观众看的精炼要点，**不是段落复制**——一句话讲一件事。
3. 第一页通常是封面，标题用文档大标题或首句主题；bullets 可以空。
4. 最后一页通常是"总结/要点回顾/致谢"，按文档语气决定。
5. `image_placeholders` 数组**永远传空**——后续版本才会真正插图。
6. 只输出 JSON 对象本身，不要包 ```json 围栏，也不要前后写解释性文字。
"""


def build_outline_messages(prompt: str) -> list[dict[str, str]]:
    """拼大纲请求：契约在 system，用户给的 prompt 模板（含 {{content}} 已替换）在 user"""
    return [
        {"role": "system", "content": OUTLINE_OUTPUT_INSTRUCTION},
        {"role": "user", "content": prompt},
    ]
