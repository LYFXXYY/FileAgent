"""
Word 文档读写服务 —— python-docx

【Java 对比】
- Document() ≈ new XWPFDocument()
- doc.paragraphs ≈ document.getParagraphsArray()
- para.add_run() ≈ para.createRun()
- run.font.color.rgb = RGBColor(0xFF, 0, 0) ≈ run.setColor("FF0000")
"""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.enum.text import WD_COLOR_INDEX  # 保留（暂不使用）
from docx.shared import RGBColor

from services.diff import compute_diff


def extract_text_from_docx(file_bytes: bytes) -> str:
    """
    从 .docx 字节流提取纯文本（段落之间用 \\n 分隔）

    BytesIO 把内存字节包装成类文件对象，python-docx 需要文件路径或类文件对象
    """
    doc = Document(BytesIO(file_bytes))
    # doc.paragraphs 只包含正文段落，不含表格；如需表格另行处理
    return "\n".join(p.text for p in doc.paragraphs)


def create_plain_docx(text: str) -> BytesIO:
    """
    把纯文本按段落写入 .docx，返回可下载的字节流

    每个 \n 分段一个 Paragraph，段内保留原样
    """
    doc = Document()
    for para in text.split("\n"):
        doc.add_paragraph(para)
    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)  # 指针回到起始位置，FileResponse 从头读
    return buf


def create_diff_docx(original: str, revised: str) -> BytesIO:
    """
    生成带红字标注的 diff Word —— 用于批改任务

    【渲染规则】
      equal   → 黑色
      insert  → 红色
      delete  → 红色 + 删除线（run.font.strike）

    按段落切分处理：diff 结果是按字符级，需要按换行符重组段落
    """
    doc = Document()
    diff = compute_diff(original, revised)

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

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# 保留常量导入避免 linter 报错（若后续需要 highlight 等效果可用）
_ = WD_COLOR_INDEX
