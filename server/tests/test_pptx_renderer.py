"""
渲染层单测（s5.4）—— 派发、路径转换、soffice 失败语义

不依赖 WSL：soffice 后端只测"命令怎么拼、失败怎么报"，subprocess 全 mock。
真渲染在 tests/test_ppt_render_smoke.py（gated）。
"""

import subprocess
from types import SimpleNamespace

import pytest

import services.pptx_renderer as renderer
from services.pptx_renderer import RenderError, render_pptx_to_images, win_to_wsl_path


def _settings(**kw):
    base = {"ppt_render_backend": "stub", "ppt_render_dpi": 100, "ppt_render_timeout": 90.0}
    base.update(kw)
    return SimpleNamespace(**base)


def _use_soffice(monkeypatch):
    """
    soffice 后端的公共前置。

    除了设置，还必须把 `win_to_wsl_path` 换成假实现：真实实现只认盘符路径，而
    `tempfile.mkdtemp()` 在 Linux / CI 上给的是 `/tmp/...`，于是代码在**碰到 subprocess
    之前**就抛"不是盘符绝对路径"。这四个用例要测的是"wsl 命令找不到 / 超时 / rc!=0 /
    命令怎么拼"，跟跑在什么系统上无关；真实主机才有的那条限制由
    `test_soffice_rejects_non_drive_tempdir` 单独钉住。
    """
    monkeypatch.setattr(renderer, "get_settings", lambda: _settings(ppt_render_backend="soffice"))
    monkeypatch.setattr(renderer, "win_to_wsl_path", lambda _p: "/mnt/c/fa_ppt_render")


# ---------- stub 派发 ----------
def test_stub_returns_placeholder_pngs(monkeypatch):
    monkeypatch.setattr(renderer, "get_settings", lambda: _settings())
    out = render_pptx_to_images(b"anything", 3)
    assert len(out) == 3
    assert all(p[:4] == b"\x89PNG" for p in out)


def test_negative_slide_count_raises(monkeypatch):
    monkeypatch.setattr(renderer, "get_settings", lambda: _settings())
    with pytest.raises(RenderError, match="负数"):
        render_pptx_to_images(b"x", -1)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setattr(renderer, "get_settings", lambda: _settings(ppt_render_backend="gpu"))
    with pytest.raises(RenderError, match="未知 ppt_render_backend"):
        render_pptx_to_images(b"x", 1)


# ---------- 路径转换 ----------
def test_win_to_wsl_path_drive_letters():
    assert win_to_wsl_path(r"D:\a\b.pdf") == "/mnt/d/a/b.pdf"
    assert win_to_wsl_path(r"c:\Temp\x y\deck.pptx") == "/mnt/c/Temp/x y/deck.pptx"


def test_win_to_wsl_path_rejects_unc():
    """UNC 路径在本机会进错 WSL 实例（见 pptx_renderer 模块 docstring），必须拒"""
    with pytest.raises(RenderError, match="盘符"):
        win_to_wsl_path(r"\\wsl.localhost\Ubuntu\tmp\a.pptx")


# ---------- soffice 失败语义（mock subprocess） ----------
def test_soffice_missing_wsl_binary_raises(monkeypatch):
    _use_soffice(monkeypatch)

    def _fnf(*a, **k):
        raise FileNotFoundError("wsl")

    monkeypatch.setattr(renderer.subprocess, "run", _fnf)
    with pytest.raises(RenderError, match="找不到 wsl"):
        render_pptx_to_images(b"x", 1)


def test_soffice_timeout_raises(monkeypatch):
    _use_soffice(monkeypatch)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=1)

    monkeypatch.setattr(renderer.subprocess, "run", _timeout)
    with pytest.raises(RenderError, match="超时"):
        render_pptx_to_images(b"x", 1)


def test_soffice_nonzero_rc_raises(monkeypatch):
    """rc!=0 → RenderError 带尾部输出，绝不静默回 stub（静默降级=骗用户渲染好了）"""
    _use_soffice(monkeypatch)
    calls = []

    def _fake_run(cmd, **k):
        calls.append(cmd)
        # 第一次是 soffice 转换，第二次是 finally 里清 profile
        if "soffice" in cmd[-1]:
            return SimpleNamespace(returncode=1, stdout=b"crash", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(renderer.subprocess, "run", _fake_run)
    with pytest.raises(RenderError, match="rc=1"):
        render_pptx_to_images(b"x", 1)


def test_soffice_command_shape(monkeypatch):
    """命令必须带独立 UserInstallation profile——默认 profile 有单实例锁，并发会假成功"""
    _use_soffice(monkeypatch)
    seen = {}

    def _fake_run(cmd, **k):
        if "soffice" in cmd[-1]:
            seen["cmd"] = cmd[-1]
            return SimpleNamespace(returncode=1, stdout=b"x", stderr=b"")  # 让它报错结束
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(renderer.subprocess, "run", _fake_run)
    with pytest.raises(RenderError):
        render_pptx_to_images(b"x", 1)

    c = seen["cmd"]
    assert "-env:UserInstallation=file:///tmp/lo_prof_" in c
    assert "--convert-to pdf" in c
    assert "/mnt/" in c  # 交换路径必须走 NTFS 挂载点，不是 UNC


def test_soffice_rejects_non_drive_tempdir(monkeypatch):
    """
    真实行为（不 mock 路径转换）：临时目录不是盘符路径就直接报错

    这条是给容器化说的——soffice 后端依赖 Windows + WSL2，Linux 里 `mkdtemp()` 给的是
    `/tmp/...`，必须**响亮失败**而不是静默退回 stub（静默降级等于骗用户渲染好了）。
    """
    monkeypatch.setattr(renderer, "get_settings", lambda: _settings(ppt_render_backend="soffice"))
    monkeypatch.setattr(renderer.tempfile, "mkdtemp", lambda **k: "/tmp/fa_ppt_render_fake")

    with pytest.raises(RenderError, match="盘符"):
        render_pptx_to_images(b"x", 1)
