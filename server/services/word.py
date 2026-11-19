"""
Word 文档读写服务 —— python-docx

【Java 对比】
- Document() ≈ new XWPFDocument()
- doc.paragraphs ≈ document.getParagraphsArray()
- para.add_run() ≈ para.createRun()
- run.font.color.rgb = RGBColor(0xFF, 0, 0) ≈ run.setColor("FF0000")
"""

from __future__ import annotations

import re
from io import BytesIO

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.shared import Pt, RGBColor

from services.diff import compute_diff


def extract_text_from_docx(file_bytes: bytes) -> str:
    """
    从 .docx 字节流提取纯文本（段落之间用 \\n 分隔）

    BytesIO 把内存字节包装成类文件对象，python-docx 需要文件路径或类文件对象
    """
    doc = Document(BytesIO(file_bytes))
    # doc.paragraphs 只包含正文段落，不含表格；如需表格另行处理
    return "\n".join(p.text for p in doc.paragraphs)


def strip_markdown(text: str) -> str:
    """
    清理 Markdown 格式符号，返回纯净文本

    处理的格式：
    - **bold** → bold
    - *italic* → italic
    - ~~strikethrough~~ → strikethrough
    - `code` → code
    - # 标题 → 标题
    - - 列表项 → 列表项（去掉 -）
    - 1. 列表项 → 列表项（去掉序号前缀）
    """
    # 先清理行内格式
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # **bold** → bold
    text = re.sub(r"\*([^*]+)\*", r"\1", text)  # *italic* → italic
    text = re.sub(r"~~([^~]+)~~", r"\1", text)  # ~~strike~~ → strike
    text = re.sub(r"`([^`]+)`", r"\1", text)  # `code` → code

    # 清理行首格式
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        # 标题标记 # ## ### 等
        if stripped.startswith("#"):
            stripped = re.sub(r"^#+\s*", "", stripped)
        # 无序列表
        elif stripped.startswith(("- ", "* ")):
            stripped = stripped[2:]
        # 有序列表 1. 2. 等
        elif re.match(r"^\d+\.\s", stripped):
            stripped = re.sub(r"^\d+\.\s*", "", stripped)
        lines.append(stripped)

    return "\n".join(lines)


def add_styled_paragraph(doc: Document, text: str, is_title: bool = False) -> None:
    """
    添加带样式的段落到文档

    - is_title=True 时使用加粗、居中对齐
    - 普通文本保持默认样式
    """
    if not text.strip():
        # 空行也要保留段落结构
        doc.add_paragraph()
        return

    para = doc.add_paragraph()
    if is_title:
        para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = para.add_run(text)
        run.bold = True
        run.font.size = Pt(14)
    else:
        para.add_run(text)


def create_plain_docx(text: str) -> BytesIO:
    """
    把纯文本按段落写入 .docx，返回可下载的字节流

    【优化】
    - 清理 Markdown 符号
    - 保持段落结构
    """
    doc = Document()

    # 清理 Markdown 格式
    clean_text = strip_markdown(text)

    for para in clean_text.split("\n"):
        doc.add_paragraph(para)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)  # 指针回到起始位置，FileResponse 从头读
    return buf


def _append_spec_refs(doc: Document, refs: list[dict]) -> None:
    """
    依据条款清单单独一页（阶段 6 A7）。

    为什么写进交付件而不是只留在界面上：批改稿常被转给别人或离线看，那时无依据可查，
    "按什么标准判的"就丢了。这一页是给下游读者的，不是给用户回忆的。

    只列条款号与标题行，不整段抄录规范正文：交付件会被外发，把用户规范库的全文塞进去
    等于替他做了一次转载。要看条文去规范库页。
    """
    doc.add_page_break()

    title = doc.add_paragraph()
    run = title.add_run("本次批改依据的规范条款")
    run.bold = True
    run.font.size = Pt(12)

    note = doc.add_paragraph()
    note_run = note.add_run("（按相关度自规范库召回，仅列条款号与标题；完整条文请在规范库查看）")
    note_run.font.size = Pt(9)

    for ref in refs:
        line = doc.add_paragraph()
        head = line.add_run(f"{ref.get('clause', '?')}　")
        head.bold = True
        excerpt = str(ref.get("excerpt", "")).strip().splitlines()
        line.add_run(excerpt[0] if excerpt else "")


def create_diff_docx(original: str, revised: str, spec_refs: list[dict] | None = None) -> BytesIO:
    """
    生成带红字标注的 diff Word —— 用于批改任务

    【渲染规则】
      equal   → 黑色
      insert  → 红色
      delete  → 红色 + 删除线（run.font.strike）

    【优化】
    - 清理 Markdown 符号，避免文档中出现 ** 等格式标记
    - 按段落切分处理：diff 结果是按字符级，需要按换行符重组段落
    - 文档末尾追加纯净版完整文本（黑色，无标注）
    """
    doc = Document()

    # 先清理 Markdown 符号，避免文档中出现格式标记
    clean_revised = strip_markdown(revised)

    # 使用清理后的文本计算 diff
    diff = compute_diff(original, clean_revised)

    # 把 diff 片段按换行拆分到多个段落，避免整篇文档挤在一个段落里
    # 缓冲区存当前段落待加的 (op, text) 片段
    current_para_runs: list[tuple[str, str]] = []

    def flush_paragraph() -> None:
        if not current_para_runs:
            return
        para = doc.add_paragraph()
        for op, text in current_para_runs:
            run = para.add_run(text)
            if op == "insert":
                run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
            elif op == "delete":
                run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
                run.font.strike = True
            # equal 保持默认黑色
        current_para_runs.clear()

    for op, text in diff:
        # equal 和 insert 的文本都可能包含换行，需要按 \n 拆开分别归入段落
        # delete 段落一般较短，也统一按 \n 处理
        if "\n" not in text:
            current_para_runs.append((op, text))
            continue
        # 含换行 → 逐段处理
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                # 遇到换行：把之前的 runs 刷到新段落，再新建空段落
                flush_paragraph()
            if part:
                current_para_runs.append((op, part))
    flush_paragraph()

    # ---------- 追加修订后的完整纯文本 ----------
    # 添加分页符，让纯净文本从新的一页开始
    doc.add_page_break()

    # 添加标题
    title_para = doc.add_paragraph()
    title_run = title_para.add_run("修订后完整文本（可直接复制）")
    title_run.bold = True
    title_run.font.size = Pt(12)

    # 添加空行分隔
    doc.add_paragraph()

    # 添加纯文本段落（黑色，无标注）
    for line in clean_revised.split("\n"):
        doc.add_paragraph(line)

    # 依据条款页：没召回就没这一页，一个字都不多写（保持与阶段 6 之前一致的交付件）
    if spec_refs:
        _append_spec_refs(doc, spec_refs)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
