"""输出契约：`--json` 时 stdout 恒为单个 JSON（N5）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from .errors import CwfError

__all__ = ["Output", "payload"]


def payload(**kwargs: Any) -> dict[str, Any]:
    """统一成功载荷：`ok` 恒在第一位。"""
    data: dict[str, Any] = {"ok": True}
    data.update(kwargs)
    return data


def _dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _no_color_env() -> bool:
    """`NO_COLOR` 只要**存在且非空**就关色（按 no-color.org 的约定）。"""
    return bool(os.environ.get("NO_COLOR"))


class Output:
    def __init__(
        self,
        *,
        json_mode: bool = False,
        json_file: str | None = None,
        quiet: bool = False,
        color: bool = True,
    ) -> None:
        self.json_mode = json_mode
        self.json_file = Path(json_file) if json_file else None
        self.quiet = quiet
        color = color and not _no_color_env()
        self.console = Console(
            color_system="standard" if color else None,
            no_color=not color,
            highlight=False,
            soft_wrap=False,
        )

    # ---- 结果 ----
    def emit(self, payload: dict[str, Any], human: Callable[[], None] | None = None) -> None:
        if self.json_file is not None:
            self._write_file(payload)
        if self.json_mode:
            sys.stdout.write(_dumps(payload) + "\n")
            sys.stdout.flush()
            return
        if human is not None:
            human()

    def _write_file(self, payload: dict[str, Any]) -> None:
        assert self.json_file is not None
        self.json_file.parent.mkdir(parents=True, exist_ok=True)
        self.json_file.write_text(_dumps(payload) + "\n", encoding="utf-8", newline="\n")

    # ---- 错误 ----
    def error(self, err: CwfError) -> None:
        if self.json_file is not None:
            self._write_file(err.to_json())
        if self.json_mode:
            sys.stdout.write(_dumps(err.to_json()) + "\n")
            sys.stdout.flush()
            return
        err_console = Console(stderr=True, no_color=not self.console.color_system)
        err_console.print(f"[red]✗[/red] {err.message}", markup=True, highlight=False)
        if err.hint:
            err_console.print(f"  [yellow]提示[/yellow] {err.hint}", highlight=False)

    # ---- 便捷打印 ----
    def line(self, text: str = "") -> None:
        self.console.print(text, highlight=False)

    def kv(self, key: str, value: str, *, ok: bool | None = None) -> None:
        """`键 值` 一行；ok=None 不加符号。--quiet 下仍然输出（那是事实，不是装饰）。"""
        mark = "" if ok is None else ("[green]✓[/green] " if ok else "[red]✗[/red] ")
        self.console.print(f"{mark}{key}  {value}", highlight=False)

    def heading(self, text: str) -> None:
        if self.quiet:
            return
        self.console.print(f"[bold]{text}[/bold]", highlight=False)
