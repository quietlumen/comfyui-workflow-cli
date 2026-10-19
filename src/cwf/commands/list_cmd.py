"""`cwf list`（PRD §7）：只读台账，不扫实例目录。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from ..context import Ctx
from ..config import load
from ..errors import not_initialized, usage
from ..ledger import Entry, iter_entries
from ..library import scan
from ..output import payload

_STATE_ORDER = ("ready", "captured", "stale", "missing", "registered")


def last_run(entry: Entry) -> dict[str, Any] | None:
    runs = entry.dir / "runs"
    if not runs.is_dir():
        return None
    files = sorted((p for p in runs.glob("*.json")), key=lambda p: p.name)
    if not files:
        return None
    from ..io_utils import read_json_or

    raw = read_json_or(files[-1], None)
    if not isinstance(raw, dict):
        return None
    return {
        "run_id": raw.get("run_id") or files[-1].stem,
        "status": raw.get("status"),
        "at": raw.get("finished_at") or raw.get("started_at"),
    }


def item(entry: Entry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.name,
        "origin": entry.origin,
        "state": entry.state,
        "nodes": (entry.meta.get("nodes") or {}).get("total", 0),
        "params": len(entry.params),
        "function": entry.function,
        "function_source": entry.meta.get("function_source", "auto"),
        "warnings": len(entry.meta.get("warnings") or []),
        "last_run": last_run(entry),
    }


def _group(entries: list[Entry]) -> list[tuple[str, list[Entry]]]:
    groups: dict[str, list[Entry]] = {}
    for e in entries:
        if e.origin == "api":
            key = "(导入)"
        else:
            rel = str((e.meta.get("source") or {}).get("rel_path") or "")
            key = str(Path(rel).parent.as_posix()) if rel else "."
            key = "." if key in ("", ".") else key
        groups.setdefault(key, []).append(e)
    return sorted(groups.items(), key=lambda kv: kv[0])


def run(
    ctx: Ctx,
    *,
    types: list[str],
    states: list[str],
    origin: str | None,
    limit: int | None,
    tree: bool,
    not_synced: bool,
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))

    if not_synced:
        if states or types or origin or limit:
            raise usage(
                "--not-synced 与 --status/--type/--origin/--limit 互斥",
                "前者列文件，后者过滤台账条目",
            )
        _list_not_synced(ctx, tree=tree)
        return

    entries = iter_entries(ws)
    if types:
        entries = [e for e in entries if e.function in types]
    if states:
        entries = [e for e in entries if e.state in states]
    if origin:
        entries = [e for e in entries if e.origin == origin]

    total = len(entries)
    if limit is not None:
        entries = entries[:limit]

    rows = [item(e) for e in entries]
    data = payload(count=total, source="ledger", items=rows)

    def human() -> None:
        out = ctx.out
        if not rows:
            out.line("台账里没有匹配的条目。")
        else:
            table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
            table.add_column("ID", no_wrap=True)
            table.add_column("显示名")
            table.add_column("来源", no_wrap=True)
            table.add_column("状态", no_wrap=True)
            table.add_column("节点", justify="right")
            table.add_column("参数", justify="right")
            table.add_column("功能", no_wrap=True)
            table.add_column("最近运行", no_wrap=True)
            grouped = _group(entries) if tree else [("", entries)]
            by_id = {r["id"]: r for r in rows}
            for group_name, group_entries in grouped:
                if tree and group_name:
                    table.add_row(f"[dim]{group_name}/[/dim]", "", "", "", "", "", "", "")
                for e in group_entries:
                    r = by_id[e.id]
                    func = r["function"] + ("*" if r["function_source"] == "manual" else "")
                    origin_cn = "文件" if r["origin"] == "instance" else "API"
                    lr = r["last_run"]
                    last = f"{lr['status']} · {str(lr['at'])[5:16]}" if lr else "—"
                    # 有警告就在状态后面挂一个 !，再靠脚注解释；不新增一列（表已经够宽）
                    state = r["state"] + ("!" if r["warnings"] else "")
                    table.add_row(
                        r["id"], r["name"], origin_cn, state,
                        str(r["nodes"]), str(r["params"]), func, last,
                    )
            out.console.print(table)

        out.line("")
        out.line(
            f"{total} 个工作流（台账）。来源：工作区台账，非实例目录；"
            "新增文件请先 cwf sync，或看 cwf list --not-synced。"
        )
        if any(r["function_source"] == "manual" for r in rows):
            out.line("* = 已人工指定的功能分类（sync 不再覆盖）")
        warned = [r for r in rows if r["warnings"]]
        if warned:
            out.line(
                f"! = 有转换警告（{len(warned)} 个）；"
                "看详情：cwf inspect <ID> --warnings"
            )

    ctx.out.emit(data, human)


def _list_not_synced(ctx: Ctx, *, tree: bool) -> None:
    ws = ctx.ws
    cfg = load(ws)
    wf_dir = Path(cfg.instance.workflows_dir) if cfg.instance.workflows_dir else None
    if wf_dir is None or not wf_dir.is_dir():
        raise usage(
            "实例工作流目录不可用，无法做差集",
            "cwf init --instance-dir <ComfyUI 目录>，或直接 cwf list 看台账",
        )
    known = {
        str((e.meta.get("source") or {}).get("rel_path"))
        for e in iter_entries(ws)
        if e.origin == "instance"
    }
    files = [f for f in scan(wf_dir) if f.rel_path not in known]
    rows = [
        {"rel_path": f.rel_path, "size": f.size, "mtime": f.mtime} for f in files
    ]
    data = payload(count=len(rows), source="instance", not_synced=rows)

    def human() -> None:
        out = ctx.out
        if not rows:
            out.line("实例目录里的文件都已经登记过了。")
        else:
            for f in (sorted(rows, key=lambda r: r["rel_path"]) if not tree else rows):
                out.line(f"  {f['rel_path']}  [dim]{f['size']} B · {f['mtime']}[/dim]")
        out.line("")
        out.line(
            f"{len(rows)} 个文件（实例目录，未登记）。来源：实例目录，只读探测；"
            "要收进台账请跑 cwf sync。"
        )

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("list")
    def list_cmd(
        ctx: typer.Context,
        type_: list[str] = typer.Option([], "--type", help="按功能分类过滤（可重复）"),
        status: list[str] = typer.Option([], "--status", help="按状态过滤（可重复）"),
        origin: str | None = typer.Option(None, "--origin", help="instance | api"),
        limit: int | None = typer.Option(None, "--limit", help="只取前 N 条"),
        tree: bool = typer.Option(False, "--tree", help="按目录分组显示"),
        not_synced: bool = typer.Option(False, "--not-synced", help="列出实例里还没登记的文件"),
    ) -> None:
        """列出台账里的工作流（不扫实例目录、离线可用）。"""
        run(
            ctx.obj,
            types=list(type_),
            states=list(status),
            origin=origin,
            limit=limit,
            tree=tree,
            not_synced=not_synced,
        )
