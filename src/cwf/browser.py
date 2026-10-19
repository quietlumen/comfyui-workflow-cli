"""浏览器就绪检测（M0 只报告状态，M2 才真正驱动 Playwright）。"""

from __future__ import annotations

import os
from pathlib import Path

# Playwright 的 `channel="chrome" / "msedge"` 会去这些位置找系统浏览器。
# 实测本机能用它转换，所以"没下载 chromium"不等于"不能转换"。
_CHANNELS = {
    "chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
    "msedge": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
}


def channel_exe(channel: str) -> str | None:
    """系统里装了的 Chrome/Edge 可执行文件；没装返回 None。"""
    for raw in _CHANNELS.get(channel, ()):
        path = Path(os.path.expandvars(raw))
        if path.is_file():
            return str(path)
    return None


def browser_ready() -> bool | None:
    """True/False；`playwright` 未安装时返回 None（表示"还没到需要它的时候"）。

    "就绪"= 有已下载的 chromium **或** 有可用的系统 Chrome/Edge
    （config.capture.browser 默认 `chrome`，走的是后者）。
    """
    try:
        import playwright  # noqa: F401
    except Exception:
        return None

    root = _browsers_root()
    if root is not None and root.is_dir() and any(p.is_dir() for p in root.iterdir()):
        return True
    return any(channel_exe(ch) for ch in _CHANNELS)


def _browsers_root() -> Path | None:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "ms-playwright"
        return None
    return Path.home() / ".cache" / "ms-playwright"


def describe(ready: bool | None) -> str:
    if ready is None:
        return "未安装 playwright（M2 转换时才需要）"
    if ready:
        names = [ch for ch in _CHANNELS if channel_exe(ch)]
        return "就绪（系统 " + "/".join(names) + "）" if names else "就绪"
    return "缺浏览器，跑 python -m playwright install chromium"
