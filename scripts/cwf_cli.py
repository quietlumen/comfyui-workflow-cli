#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cwf skill 入口 —— 薄壳转发（内核在 skill 内，配置过即用）。

流程：
  1) 读 skill 根目录的 skill_env.json（setup.py 写的）：
       {"python": "<.venv 的 python>", "workspace": "<工作区目录>"}
  2) 用该 python 跑 `python -m cwf`，并让 Python 能找到 skill 内的内核
     （PYTHONPATH 指向 <skill>/src）。
  3) 未配置（skill_env.json 不存在）时，提示先跑 setup.py。
  4) --workspace 缺省时用 skill_env.json 里的值；显式给了就覆盖。

stdout 恒为单个 JSON（追加 --json）；日志/错误走 stderr。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = SKILL_DIR / "src"
ENV_FILE = SKILL_DIR / "skill_env.json"


def _load_env() -> dict:
    if not ENV_FILE.is_file():
        return {}
    try:
        raw = json.loads(ENV_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


_GATE_FREE = {"--help", "--version", "init"}


def _first_nonflag(argv: list[str]) -> str:
    """取第一个非 -- 开头的参数（子命令名）；找不到返回 ''。

    带值全局选项（--workspace / --json-file）连值一起跳过，避免把路径当命令。
    """
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--workspace", "--json-file") and i + 1 < len(argv):
            i += 2
            continue
        if arg.startswith("--"):
            i += 1
            continue
        return arg
    return ""


def _gate(argv: list[str], env: dict) -> int | None:
    """门禁：未配置 / 未初始化时拦截后续动作。

    返回 None = 放行；返回 int = 直接退出（已输出 JSON）。
    注意：`init` 需要特殊处理——未初始化时它要放行，但之后会卡在"未配置"检查，
    所以 init 一律先放行到内核（内核自己会报缺参数/缺 workspace）。
    """
    import json as _json

    cmd = _first_nonflag(argv)

    # 0) init：永远放行（它本身就是初始化动作；配置检查交给 setup.py 的场景）
    if cmd == "init":
        return None

    # 1) 未配置（缺 skill_env.json / 缺 .venv python）
    py = env.get("python")
    if not py or not Path(py).is_file():
        if cmd in _GATE_FREE or cmd == "":
            return None
        sys.stderr.write("cwf skill 还没配置。先跑：\n"
                         f"  {SKILL_DIR / 'scripts' / 'setup.py'} --workspace <目录> --instance-dir <ComfyUI>\n")
        sys.stdout.write(_json.dumps(
            {"ok": False, "error": "未配置：先运行 scripts/setup.py", "error_code": "NOT_CONFIGURED"},
            ensure_ascii=False))
        return 1

    # 2) 已配置但工作区未初始化（缺 config.json）——连查看工作流都不行
    #    注意：init 已在上面的第 0 步放行，这里只拦"想用但没初始化"的命令。
    ws_value = None
    for i, arg in enumerate(argv):
        if arg == "--workspace" and i + 1 < len(argv):
            ws_value = argv[i + 1]
            break
    ws_root = Path(ws_value).expanduser() if ws_value else None
    if ws_root is not None and not (ws_root / "config.json").is_file():
        sys.stderr.write(
            f"工作区未初始化：{ws_root} 没有 config.json。\n"
            f"先跑：{SKILL_DIR / 'scripts' / 'setup.py'} --workspace {ws_root} --instance-dir <ComfyUI>\n"
            "或 cwf init --workspace <目录> --instance-dir <ComfyUI>。\n"
        )
        sys.stdout.write(_json.dumps(
            {"ok": False, "error": f"工作区未初始化：{ws_root}", "error_code": "NOT_INITIALIZED"},
            ensure_ascii=False))
        return 1

    return None


def _pass_through(py: str, argv: list[str]) -> int:
    """把参数原样交给内核（--help / --version 等不注入 --json）。"""
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(SRC_DIR) + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run([py, "-m", "cwf"] + argv,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = _load_env()

    # --help / --version：不需要配置与 workspace。已配置 → 透传内核；
    # 未配置 → 薄壳自己给命令清单（内核依赖还没装，透传只会报 ImportError）。
    if "--help" in argv or "--version" in argv:
        py = env.get("python")
        if py and Path(py).is_file():
            return _pass_through(py, argv)
        if "--version" in argv:
            sys.stdout.write("cwf skill 0.1.0（未配置，先跑 scripts/setup.py）\n")
            return 0
        sys.stdout.write(
            "cwf —— ComfyUI 工作流命令行调用器（agent skill 版，尚未配置）\n"
            "先跑：scripts/setup.py --workspace <目录> --instance-dir <ComfyUI>\n"
            "配置后可用命令：init / status / sync / convert / list / show / inspect /\n"
            "  param / run / batch / queue / history / log / upload / set / remove\n"
        )
        return 0

    # 归一 workspace（门禁里要用；init 也走这条）
    ws_index = None
    for i, arg in enumerate(argv):
        if arg == "--workspace" and i + 1 < len(argv):
            ws_index = i
            break
    if ws_index is None:
        default_ws = str(env.get("workspace") or "")
        if default_ws:
            argv = ["--workspace", default_ws] + argv
        else:
            argv = ["--workspace", str(Path.cwd())] + argv
    else:
        ws = Path(argv[ws_index + 1]).expanduser()
        if not ws.is_absolute():
            ws = Path.cwd() / ws
        argv[ws_index + 1] = str(ws)

    # 门禁：未配置 / 未初始化一律拦截（init 除外）
    blocked = _gate(argv, env)
    if blocked is not None:
        return blocked

    py = env.get("python")

    if "--json" not in argv:
        argv = ["--json"] + argv

    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(SRC_DIR) + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    cmd = [py, "-m", "cwf"] + argv
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=child_env)
    except OSError as exc:
        sys.stdout.write('{"ok": false, "error": "启动 cwf 失败: %s"}' % str(exc).replace('"', "'"))
        return 1

    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
