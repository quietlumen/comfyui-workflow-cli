"""命令树：全局选项、异常→退出码、命令注册（附录 §7.1）。

不依赖 click（typer 0.27 起自带 CLI 核），只用 typer 的公开异常类型，
这样升级 typer 不会连带改动错误处理。
"""

from __future__ import annotations

import sys
import traceback

import typer

from . import __version__
from .context import Ctx
from .errors import CwfError, ExitCode
from .output import Output
from .paths import Workspace, resolve_workspace

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="cwf —— 把 ComfyUI 工作流变成命令行可参数化调用的函数。",
)


def _wants_json(argv: list[str]) -> bool:
    return "--json" in argv or "--json-file" in argv


_VALUE_GLOBALS = ("--workspace", "--json-file")
_FLAG_GLOBALS = ("--json", "--quiet", "--yes", "--no-color", "--debug")


def _hoist_globals(argv: list[str]) -> list[str]:
    """把全局选项挪到命令前。

    typer 只认"命令之前"的全局选项，但 `cwf init --instance-dir X --workspace Y`
    是人最自然的写法。这里搬带路径值的选项与几个纯 flag；同名的子命令选项
    语义一致（都是全局开关），搬过去不会改变行为。
    """
    moved: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _VALUE_GLOBALS and i + 1 < len(argv):
            moved.extend([arg, argv[i + 1]])
            i += 2
            continue
        if any(arg.startswith(g + "=") for g in _VALUE_GLOBALS):
            moved.append(arg)
            i += 1
            continue
        if arg in _FLAG_GLOBALS:
            moved.append(arg)
            i += 1
            continue
        rest.append(arg)
        i += 1
    return moved + rest


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    workspace: str | None = typer.Option(None, "--workspace", help="工作区目录（默认 D:\\comfyui_workflow\\data 或 $CWF_HOME）"),
    json_mode: bool = typer.Option(False, "--json", help="stdout 输出单个 JSON"),
    json_file: str | None = typer.Option(None, "--json-file", help="同时把 JSON 写入文件"),
    quiet: bool = typer.Option(False, "--quiet", help="只输出结果行"),
    yes: bool = typer.Option(False, "--yes", help="跳过交互确认"),
    no_color: bool = typer.Option(False, "--no-color", help="禁用颜色"),
    debug: bool = typer.Option(False, "--debug", help="输出堆栈并写 logs/cwf.log"),
    version: bool = typer.Option(False, "--version", help="打印版本后退出"),
) -> None:
    if version:
        typer.echo(f"cwf {__version__}")
        raise typer.Exit(ExitCode.OK)
    ctx.obj = Ctx(
        ws=Workspace(resolve_workspace(workspace)),
        out=Output(
            json_mode=json_mode,
            json_file=json_file,
            quiet=quiet,
            color=not no_color,
        ),
        yes=yes,
        debug=debug,
    )


def _register() -> None:
    from .commands import (
        init_cmd,
        list_cmd,
        remove_cmd,
        set_cmd,
        show_cmd,
        status_cmd,
        sync_cmd,
        convert_cmd,
        inspect_cmd,
        param_cmd,
        run_cmd,
        history_cmd,
        batch_cmd,
        queue_cmd,
        upload_cmd,
    )

    init_cmd.register(app)
    status_cmd.register(app)
    sync_cmd.register(app)
    convert_cmd.register(app)
    inspect_cmd.register(app)
    param_cmd.register(app)
    run_cmd.register(app)
    history_cmd.register(app)
    batch_cmd.register(app)
    queue_cmd.register(app)
    upload_cmd.register(app)
    list_cmd.register(app)
    show_cmd.register(app)
    set_cmd.register(app)
    remove_cmd.register(app)


_register()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv == ["--json"]:
        argv = [a for a in argv if a != "--json"] + ["--help"]
    debug = "--debug" in argv
    try:
        app(args=_hoist_globals(argv), standalone_mode=False)
        return int(ExitCode.OK)
    except CwfError as err:
        if debug:
            _log_debug(argv, f"{err.error_code}: {err.message}\n{err.hint or ''}")
        _print_error(argv, err)
        return int(err.code)
    except typer.Exit as exc:                     # --help / --version / 显式退出
        return int(exc.exit_code or 0)
    except typer.TyperException as exc:           # 参数/用法错误
        if debug:
            _log_debug(argv, f"USAGE: {exc}")
        _print_usage(argv, exc)
        return int(ExitCode.USAGE)
    except typer.Abort:                           # Ctrl+C / 用户取消
        return int(ExitCode.NEED_CONFIRM)
    except Exception as exc:                      # 未预期异常：不吞，但要给出退出码
        if debug:
            traceback.print_exc()
            _log_debug(argv, traceback.format_exc())
        _print_error(
            argv,
            CwfError(
                f"内部错误：{exc}",
                code=ExitCode.ERROR,
                error_code="INTERNAL",
                hint="用 --debug 看堆栈",
            ),
        )
        return int(ExitCode.ERROR)


def _print_error(argv: list[str], err: CwfError) -> None:
    out = Output(json_mode=_wants_json(argv), quiet=True, color=False)
    out.error(err)


def _workspace_from(argv: list[str]) -> Workspace:
    """异常发生时 `Ctx` 已经拿不到了，只能从 argv 里再抠一次工作区。"""
    for i, arg in enumerate(argv):
        if arg == "--workspace" and i + 1 < len(argv):
            return Workspace(resolve_workspace(argv[i + 1]))
        if arg.startswith("--workspace="):
            return Workspace(resolve_workspace(arg.split("=", 1)[1]))
    return Workspace(resolve_workspace(None))


def _log_debug(argv: list[str], text: str) -> None:
    """`--debug` 时把这次失败追加进 `logs/cwf.log`（附录 §7.1）。

    写不进去就静默放弃：日志失败不该盖住真正的错误。
    """
    from datetime import datetime

    try:
        ws = _workspace_from(argv)
        ws.logs_dir.mkdir(parents=True, exist_ok=True)
        with ws.log_file.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  cwf {' '.join(argv)}\n")
            fh.write(text.rstrip() + "\n")
    except OSError:
        pass


def _print_usage(argv: list[str], err: Exception) -> None:
    message = str(err) or err.__class__.__name__
    if _wants_json(argv):
        Output(json_mode=True).error(
            CwfError(message, code=ExitCode.USAGE, error_code="USAGE")
        )
    else:
        sys.stderr.write(f"用法错误：{message}\n")
