"""核查文档里的 `文件:符号` 引用是否还指得到东西。

为什么要有这个脚本：AGENTS.md §6 定的规矩是"引用代码位置用符号名，不写行号"，理由是
行号随任何一次改动腐烂。但符号名也会腐烂——模块被删（阶段 5 的 ppt_graph / pptx_builder
就是）、函数被改名。这条检查能在三十秒内告诉你断了几条，不用等下一个会话照着文档去
找一个已经不存在的东西。

用法：
    cd server && uv run python check_doc_refs.py

判定口径：
- 只判"文件在不在 + 符号名在不在该文件里"，是存在性检查，不判语义对不对。
  文档里的行为描述、阈值含义、决策理由仍然要人读——机器管不了那部分。
- 报出来的条目不等于"文档错了"：指向已删模块的历史归档（SPEC 路线表、缺陷台账）
  是对的，需要人工看上下文。脚本的价值是把"要人看的地方"从全量缩到几条。
"""

import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

# 脚本在 server/ 下，文档在仓库根
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ["AGENTS.md", "SPEC.md", "README.md", "UI-REFERENCE.md"]

REF = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|vue|ts|json|toml|md))[:：]([A-Za-z_][A-Za-z0-9_.]*)")

SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", ".venv", "venv", "uploads", "outputs"}

# 文档里当反面例子写的占位符，不是真文件
PLACEHOLDERS = {("file.py", "NN")}

# 阶段 5 大纲流水线的模块已于 2026-11-21 整体 git rm，SPEC §9.12 与路线表仍在讲那段历史。
# 那是归档不是谎言，所以显式列出来，让工具的输出每次都只剩"真的需要看一眼"的条目。
# 加新条目要附一句为什么它是历史而非笔误；否则这个清单会变成ignore-all。
ARCHIVED = {
    ("ppt_graph.py", "plan_slides"),
    ("pptx_builder.py", "build_pptx"),
    ("vlm_critique.py", "Critique"),
}


def find_files(base: str) -> list[str]:
    """按文件名在仓库里找真实路径（文档写的相对路径常常省略前缀）"""
    hits = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if base in files:
            hits.append(os.path.join(root, base))
    return hits


def main() -> int:
    checked = 0
    archived_skipped = 0
    missing_file = []
    missing_symbol = []
    seen = set()

    for doc in DOCS:
        path = os.path.join(ROOT, doc)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for m in REF.finditer(text):
            rel, symbol = m.group(1), m.group(2)
            if (os.path.basename(rel), symbol) in PLACEHOLDERS:
                continue
            # 文档里既可能写 graphs/ppt_graph.py 也可能只写 ppt_graph.py，两种都认
            leaf = symbol.split(".")[-1]
            if (rel, leaf) in ARCHIVED or (os.path.basename(rel), leaf) in ARCHIVED:
                archived_skipped += 1
                continue
            key = (doc, rel, symbol)
            if key in seen:
                continue
            seen.add(key)
            checked += 1

            files = find_files(os.path.basename(rel))
            if not files:
                missing_file.append((doc, rel, symbol))
                continue

            leaf = symbol.split(".")[-1]
            hit = False
            for fp in files:
                try:
                    with open(fp, encoding="utf-8", errors="ignore") as fh:
                        if leaf in fh.read():
                            hit = True
                            break
                except OSError:
                    continue
            if not hit:
                missing_symbol.append((doc, rel, symbol))

    print(f"共判定 {checked} 条引用（按 文档+路径+符号 去重）")
    print(f"  另有 {archived_skipped} 条指向已删模块，按历史归档跳过（见 ARCHIVED）")
    print(f"  路径找不到：{len(missing_file)}")
    print(f"  文件在但符号没了：{len(missing_symbol)}")
    for doc, rel, symbol in missing_file:
        print(f"  [路径] {doc}  {rel}:{symbol}")
    for doc, rel, symbol in missing_symbol:
        print(f"  [符号] {doc}  {rel}:{symbol}")
    if not missing_file and not missing_symbol:
        print("  引用全部指得到东西")
    return 0


if __name__ == "__main__":
    sys.exit(main())
