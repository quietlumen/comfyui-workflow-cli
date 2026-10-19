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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = _load_env()
    py = env.get("python")
    if not py or not Path(py).is_file():
        sys.stderr.write("cwf skill 还没配置。先跑：\n"
                         f"  {SKILL_DIR / 'scripts' / 'setup.py'} --workspace <目录> --instance-dir <ComfyUI>\n")
        sys.stdout.write('{"ok": false, "error": "未配置：先运行 scripts/setup.py"}')
        return 1

    # --workspace：缺省用配置值；显式给出则覆盖
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

    if "--json" not in argv:
        argv = ["--json"] + argv

    # 工作区已初始化才能调；未初始化时明确提示"先 setup.py 或 cwf init"
    import json as _json
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
