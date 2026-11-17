"""
PPTX → 图片渲染（阶段 5 s5.4）

【两个后端，settings.ppt_render_backend 派发】
- `stub`（默认）：返回 N 张 1×1 透明 PNG。单测 / 无 LibreOffice 的机器用，e2e 配 mock VLM
  只看 prompt 元数据也够用。
- `soffice`：经 WSL 里的 LibreOffice headless 真转换（pptx → PDF → pymupdf 渲 PNG）。
  本机布局：Windows 起后端、Ubuntu(WSL2) 装 libreoffice-impress，文件经 `/mnt/d`
  这类 NTFS 挂载点交换——**不走 `\\\\wsl.localhost` UNC**：本机上 Windows 原生进程
  （python/pwsh）看到的 wsl.localhost 视图和 `wsl.exe` 会话看到的是两个不同实例，
  UNC 写的文件 `wsl` 侧看不见（2026-11-17 实测）。`/mnt/<盘>` 指向真实 NTFS，天然共享。

【签名硬约束（SPEC §9.12.2.C）】
`render_pptx_to_images(pptx_bytes, slide_count) -> list[bytes]` 是 s5.4 的切换点。
换实现（LibreOffice on Windows、pdf2image、远程渲染服务）只换函数体，
graphs/ppt_graph.py 不动、单测不动。

【为什么 soffice 每次都开独立 UserInstallation profile】
LibreOffice 的默认用户配置带单实例锁：批评循环并发或两次请求重叠时，第二个 soffice
会静默把活儿交给第一个实例然后立刻退出，产出对不上。独立 profile
（file:///tmp/lo_prof_<uuid>）彻底避开，代价是每次冷启动多 ~1 秒。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid

import pymupdf

from config import get_settings

# 1×1 全透明 PNG：stub 后端的占位图。PIL/OpenCV 都解得开，VLM mock 端不会因"图坏了"报错
_STUB_PNG_BYTES: bytes = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x00\x00\x00\x0dIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class RenderError(Exception):
    """渲染层失败统一出口。调用方（批评循环 render 节点）接住转 _mark_failed"""


def render_pptx_to_images(pptx_bytes: bytes, slide_count: int) -> list[bytes]:
    """
    把 PPTX 二进制每页渲成一张 PNG

    Args:
        pptx_bytes: services.pptx_builder.build_pptx 的输出
        slide_count: 页数。stub 用它决定 list 长度；soffice 以 PDF 实际页数为准
    """
    if slide_count < 0:
        raise RenderError(f"slide_count 不能是负数：{slide_count}")
    settings = get_settings()
    if settings.ppt_render_backend == "stub":
        return [_STUB_PNG_BYTES] * slide_count
    if settings.ppt_render_backend == "soffice":
        return _render_via_soffice(pptx_bytes, settings)
    raise RenderError(f"未知 ppt_render_backend：{settings.ppt_render_backend!r}")


def is_render_stub() -> bool:
    """供上层日志/UI 判断"现在的渲染是占位还是真实图""" ""
    return get_settings().ppt_render_backend == "stub"


def win_to_wsl_path(win_path: str) -> str:
    """
    Windows 绝对路径 → WSL 挂载点路径（D:\\a\\b → /mnt/d/a/b）

    只处理盘符路径；UNC 或其他形式直接抛——本项目的交换目录永远在某个盘上。
    """
    p = win_path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if not m:
        raise RenderError(f"不是盘符绝对路径，无法转 WSL 路径：{win_path}")
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}"


def _render_via_soffice(pptx_bytes: bytes, settings) -> list[bytes]:
    """经 WSL LibreOffice 真渲染。任何一步不成就 RenderError，绝不静默回 stub"""
    work = tempfile.mkdtemp(prefix="fa_ppt_render_")
    tag = uuid.uuid4().hex[:8]
    pptx_win = os.path.join(work, f"deck_{tag}.pptx")
    pdf_win = os.path.join(work, f"deck_{tag}.pdf")
    wsl_work = win_to_wsl_path(work)
    wsl_profile = f"/tmp/lo_prof_{tag}"

    try:
        with open(pptx_win, "wb") as f:
            f.write(pptx_bytes)

        # 路径带空格的概率不为零，WSL 侧参数统一单引号包住
        cmd = (
            f"soffice --headless -env:UserInstallation=file://{wsl_profile} "
            f"--convert-to pdf --outdir '{wsl_work}' '{wsl_work}/deck_{tag}.pptx'"
        )
        try:
            r = subprocess.run(
                ["wsl", "-e", "bash", "-lc", cmd],
                capture_output=True,
                timeout=settings.ppt_render_timeout,
            )
        except FileNotFoundError as e:
            raise RenderError(
                "找不到 wsl 命令：soffice 后端需要 Windows + WSL2 + libreoffice-impress"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RenderError(
                f"soffice 渲染超时（{settings.ppt_render_timeout:.0f}s）——首次冷启动慢，"
                "可上调 PPT_RENDER_TIMEOUT"
            ) from e

        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        if r.returncode != 0 or not os.path.exists(pdf_win):
            raise RenderError(f"soffice 转换失败 rc={r.returncode}：{out[-300:] or '(无输出)'}")

        try:
            doc = pymupdf.open(pdf_win)
        except Exception as e:  # PDF 存在但坏了
            raise RenderError(f"soffice 产出的 PDF 打不开：{e}") from e
        try:
            return [
                doc[i].get_pixmap(dpi=settings.ppt_render_dpi).tobytes("png")
                for i in range(doc.page_count)
            ]
        finally:
            doc.close()
    finally:
        # 临时目录 + WSL profile 都要清：profile 在 WSL 的 tmpfs 里，不攒僵尸
        shutil.rmtree(work, ignore_errors=True)
        try:
            subprocess.run(
                ["wsl", "-e", "bash", "-c", f"rm -rf {wsl_profile}"],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass  # 清理失败不值得把渲染成功判成失败
