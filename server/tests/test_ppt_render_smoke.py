"""
真渲染 smoke（s5.4 gated）—— 需要 WSL + libreoffice-impress

机器上没有就整文件 skip，不进 CI 关键路径：
  wsl -e bash -lc "soffice --version"   手动排查用
"""

import subprocess

import pytest

from schemas.ppt_outline import PptOutline, PptSlide
from services.pptx_builder import build_pptx
from services.pptx_renderer import RenderError, render_pptx_to_images, win_to_wsl_path

_HAS_WSL = True
try:
    r = subprocess.run(
        ["wsl", "-e", "bash", "-lc", "soffice --version"],
        capture_output=True,
        timeout=20,
    )
    _HAS_WSL = r.returncode == 0
except Exception:
    _HAS_WSL = False

pytestmark = pytest.mark.skipif(not _HAS_WSL, reason="本机无 WSL + soffice，跳过真渲染 smoke")


def test_real_render_two_page_chinese_deck(monkeypatch):
    """2 页中文 pptx → 真渲染 → 页数一致、PNG 尺寸与 DPI 相称、非全透明（有内容）

    显式强制 soffice 后端——不依赖测试机的 .env 设置
    """
    from types import SimpleNamespace

    import services.pptx_renderer as renderer

    monkeypatch.setattr(
        renderer,
        "get_settings",
        lambda: SimpleNamespace(
            ppt_render_backend="soffice", ppt_render_dpi=100, ppt_render_timeout=90.0
        ),
    )
    outline = PptOutline(
        title="渲染 smoke",
        slides=[
            PptSlide(
                title="第一页：标题压顶测试",
                bullets=["要点甲", "要点乙"],
                image_placeholders=[],
            ),
            PptSlide(title="第二页", bullets=["Hello 中英混排 123"], image_placeholders=[]),
        ],
        degraded=False,
    )
    pptx = build_pptx(outline)
    # 3 页 = 封面 + 2 正文
    images = render_pptx_to_images(pptx, slide_count=3)

    assert len(images) == 3
    for png in images:
        assert png[:4] == b"\x89PNG"
        # 100 DPI × 10 英寸默认幻灯片 → 宽 1000px；stub 的 1×1 会远小于这个数
        assert len(png) > 3000, f"PNG 太小（{len(png)}B），像 stub 占位而不是真渲染"


def test_win_to_wsl_path_roundtrip_readable():
    """转出去的 /mnt 路径 WSL 侧真能 stat——防止挂载点假设过期"""
    probe = win_to_wsl_path(r"D:\Projects\FileAgent\server\.wsl_path_probe.txt")
    with open(r"D:\Projects\FileAgent\server\.wsl_path_probe.txt", "w") as f:
        f.write("probe")
    try:
        r = subprocess_run_cat(probe)
        assert "probe" in r
    finally:
        import os

        os.remove(r"D:\Projects\FileAgent\server\.wsl_path_probe.txt")


def subprocess_run_cat(wsl_path: str) -> str:
    import subprocess

    out = subprocess.run(
        ["wsl", "-e", "bash", "-lc", f"cat '{wsl_path}'"],
        capture_output=True,
        timeout=15,
    )
    if out.returncode != 0:
        raise RenderError(f"WSL 读不到 {wsl_path}：{out.stderr.decode('utf-8', 'replace')[:200]}")
    return out.stdout.decode("utf-8", "replace")
