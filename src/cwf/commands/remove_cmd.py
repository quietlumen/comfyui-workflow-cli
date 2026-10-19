"""`cwf remove`（PRD §7）：删台账条目，不动实例源文件、默认不动运行记录。"""

from __future__ import annotations

import shutil

import typer

from ..context import Ctx
from ..errors import not_initialized
from ..ledger import iter_entries
from ..output import payload
from ..prompts import require_yes
from ..resolve import resolve

ENTRY_FILES = ("meta.json", "params.json", "source.json", "api.json", "presets.json")


def run(ctx: Ctx, target: str, *, purge: bool) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    entry = resolve(iter_entries(ws), target)

    runs_dir = entry.dir / "runs"
    run_count = len(list(runs_dir.glob("*.json"))) if runs_dir.is_dir() else 0
    details = [
        f"将删除台账条目 {entry.id}（{entry.name}）",
        "实例目录里的源文件不动",
    ]
    if purge:
        details.append(f"并删除 {run_count} 条运行记录（--purge，不可恢复）")
    else:
        details.append(f"保留 {run_count} 条运行记录（history/log 仍可查）")
    if entry.params:
        details.append(f"同时丢弃 {len(entry.params)} 个参数绑定")
    require_yes(ctx, f"删除条目 {entry.id}？", details=details)

    removed: list[str] = []
    if purge:
        shutil.rmtree(entry.dir, ignore_errors=True)
        removed.append(str(entry.dir))
    else:
        for name in ENTRY_FILES:
            path = entry.dir / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        # 目录里只剩 runs/（或干脆空了）
        if entry.dir.is_dir() and not any(entry.dir.iterdir()):
            entry.dir.rmdir()

    data = payload(
        id=entry.id,
        name=entry.name,
        purged=purge,
        removed=removed,
        runs_kept=0 if purge else run_count,
        params_dropped=len(entry.params),
    )

    def human() -> None:
        out = ctx.out
        out.line(f"[green]✓[/green] 已删除台账条目 {entry.id}（{entry.name}）")
        if purge:
            out.line("  运行记录也一起删了。")
        elif run_count:
            out.line(f"  保留 {run_count} 条运行记录：cwf history {entry.id}")
        out.line("  实例里的源文件还在，下次 cwf sync 会重新登记（分配新 ID）。")

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("remove")
    def remove_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="ID / 别名 / 名字 / 唯一前缀"),
        purge: bool = typer.Option(False, "--purge", help="连运行记录一起删（不可恢复）"),
    ) -> None:
        """删除台账条目（不动实例源文件；默认保留 runs/）。"""
        run(ctx.obj, target, purge=purge)
