#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cwf skill 一次性配置：建 .venv、离线装依赖、初始化工作区。

用法：
    python setup.py --workspace D:\\xxx [--instance-dir D:\\ComfyUI\\ComfyUI]
                    [--host H] [--port P] [--base-python X]

做的事情（幂等，可重复跑）：
  1. 在 skill 目录建 `.venv`（用 base python；缺省优先捆绑 Python 3.12，其次系统 python）；
  2. 用 pip 在线安装 cwf 依赖（typer/rich/httpx/pydantic/playwright）；
  3. 把".venv 里的 python" 记录成命令入口，供 scripts/cwf_cli.py 调用；
  4. 在指定工作区目录跑 `cwf init`（**--workspace 必填**，由用户提供）。
     init 只写 config.json（instance 目录/host/port），不会扫任何东西。

每次跑都会把 3/4 两步的"当前配置"写进 skill 根目录的 `skill_env.json`，
scripts/cwf_cli.py 每次调用都会读它——这就是"skill 内命令配置"。

关于"日志、运行记录放哪"：cwf 的所有产物（运行记录、产物、队列、日志）都在
工作区目录里，init 时通过 --workspace 指定（用户给哪个就用哪个）。
skill 本身只放源码与 .venv。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = SKILL_DIR / "src"
VENV_DIR = SKILL_DIR / ".venv"
ENV_FILE = SKILL_DIR / "skill_env.json"

BUNDLED_PY = Path(
    r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python"
) / "python.exe"


def _base_python(chosen: str | None) -> str | None:
    if chosen:
        p = Path(chosen).expanduser()
        return str(p) if p.is_file() else None
    if BUNDLED_PY.is_file():
        return str(BUNDLED_PY)
    return shutil.which("python")


def ensure_venv(base: str) -> Path:
    if (VENV_DIR / "Scripts" / "python.exe").is_file():
        return VENV_DIR / "Scripts" / "python.exe"
    # venv.EnvBuilder.create 没有 python 参数；直接调用 base 解释器的 -m venv
    subprocess.run([base, "-m", "venv", str(VENV_DIR)], check=True,
                   capture_output=True, text=True, timeout=300)
    return VENV_DIR / "Scripts" / "python.exe"


def ensure_deps(py: Path) -> None:
    try:
        subprocess.run(
            [str(py), "-c", "import typer, rich, httpx, pydantic"],
            capture_output=True, text=True, timeout=120, check=True,
        )
        return
    except Exception:
        pass
    reqs = ["typer", "rich", "httpx", "pydantic"]
    print("      在线安装依赖（pip install）")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "--timeout", "60", *reqs],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise SystemExit(f"在线装依赖失败：{proc.stderr[-800:]}")
    # 转换（convert/sync --convert）需要 playwright；也在线装
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "--timeout", "60", "playwright"],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise SystemExit(f"在线装 playwright 失败：{proc.stderr[-800:]}")


def cwf_init(py: Path, workspace: Path, instance_dir: str | None,
             host: str | None, port: int | None) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    cmd = [str(py), "-m", "cwf", "init",
           "--workspace", str(workspace)]
    if instance_dir:
        cmd += ["--instance-dir", instance_dir]
    if host:
        cmd += ["--host", host]
    if port:
        cmd += ["--port", str(port)]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=env)
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"cwf init 失败（退出码 {proc.returncode}）")


def configure_browser(workspace: Path) -> None:
    """init 之后：把 capture.browser 设成系统里的 Chrome/Edge，免下载浏览器。

    cwf 默认 capture.browser=chromium（要 `playwright install` 下载）；
    机器上装了系统浏览器就能直接转换。探测顺序：chrome → msedge。
    """
    cfg_path = workspace / "config.json"
    if not cfg_path.is_file():
        return
    sys.path.insert(0, str(SRC_DIR))
    try:
        from cwf import browser as cwf_browser
    except Exception:
        return
    for name in ("chrome", "msedge"):
        if not cwf_browser.channel_exe(name):
            continue
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        raw.setdefault("capture", {})["browser"] = name
        tmp = cfg_path.with_name(cfg_path.name + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
        os.replace(tmp, cfg_path)
        print(f"      capture.browser -> {name}（用系统浏览器，免下载）")
        return
    print("      未发现系统 Chrome/Edge；保持 chromium（需 python -m playwright install chromium）")


def _resolve_workspace(cli_value: str | None) -> Path:
    """用户提供 workspace：给了就用；终端下没给就交互询问；非终端（agent 场景）报错。

    agent 场景一律由 agent 先用 ask_user_question 问用户，再把值当 --workspace 传进来；
    这里只兜底：交互终端可直接输入。
    """
    if cli_value:
        return Path(cli_value).expanduser()
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if interactive:
        try:
            value = input("工作区目录（日志/运行记录/产物/队列/台账放这里，必填）：").strip()
        except EOFError:
            raise SystemExit("没给工作区目录，退出。")
        if not value:
            raise SystemExit("没给工作区目录，退出。")
        return Path(value).expanduser()
    raise SystemExit(
        "缺少 --workspace（必填，用户提供）。\n"
        "agent 场景：先用 ask_user_question 向用户要工作区目录，再带上 --workspace 重跑。\n"
        "例：python setup.py --workspace D:\\xxx --instance-dir D:\\ComfyUI\\ComfyUI"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="cwf skill 配置（幂等）")
    ap.add_argument("--workspace", default=None,
                    help="工作区目录（必填；产物/运行记录/日志/队列/台账都放这里）")
    ap.add_argument("--instance-dir", default=None, help="ComfyUI 安装目录")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--base-python", default=None, help="用来建 .venv 的 python")
    args = ap.parse_args()

    workspace = _resolve_workspace(args.workspace)

    base = _base_python(args.base_python)
    if base is None:
        raise SystemExit("找不到可用的 python（捆绑 Python 与系统 python 都没有）")

    print(f"[1/4] 建 .venv（base={base}）")
    py = ensure_venv(base)
    print(f"      -> {py}")

    print("[2/4] 在线装依赖（pip install）")
    ensure_deps(py)

    print("[3/4] 记录命令入口")
    ENV_FILE.write_text(
        json.dumps({"python": str(py), "workspace": str(workspace.resolve())},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[4/4] cwf init --workspace {workspace}")
    cwf_init(py, workspace, args.instance_dir, args.host, args.port)

    print("[5/5] 浏览器配置")
    configure_browser(Path(args.workspace))

    print("\n完成。之后用 scripts/cwf_cli.py 调命令；运行记录/产物/日志都在工作区目录里。")


if __name__ == "__main__":
    main()
