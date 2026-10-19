"""`cwf show`（PRD §7）：条目元信息，只读台账。"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..context import Ctx
from ..errors import not_initialized
from ..executor import load_presets
from ..ledger import Entry, iter_entries, recent_runs, state_reasons
from ..resolve import resolve


def detail(entry: Entry) -> dict[str, Any]:
    meta = entry.meta
    source = meta.get("source")
    return {
        "ok": True,
        "id": entry.id,
        "name": entry.name,
        "aliases": meta.get("aliases") or [],
        "origin": entry.origin,
        "function": entry.function,
        "function_source": meta.get("function_source", "auto"),
        "function_evidence": meta.get("function_evidence"),
        "state": entry.state,
        "state_reasons": state_reasons(meta, entry.params, entry.api_file),
        "source": source,
        "format": meta.get("format"),
        "api_sha256": meta.get("api_sha256"),
        "api_origin": meta.get("api_origin"),
        "api_at": meta.get("api_at"),
        "api_stale": bool(meta.get("api_stale")),
        "verified": bool(meta.get("verified")),
        "nodes": meta.get("nodes") or {},
        "params": [
            {
                "name": p.get("name"),
                "type": p.get("type"),
                "category": p.get("category"),
                "default": p.get("default"),
                "key": p.get("key"),
                "node": p.get("node"),
                "input": p.get("input"),
                "orphan": bool(p.get("orphan")),
            }
            for p in entry.params
        ],
        "warnings": meta.get("warnings") or [],
        "presets": load_presets(entry),
        "runs": recent_runs(entry, 5),
    }


def run(ctx: Ctx, target: str) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    entry = resolve(iter_entries(ws), target)
    data = detail(entry)

    def human() -> None:
        out = ctx.out
        out.heading(f"{data['id']}  {data['name']}")
        out.kv("来源", "实例文件（instance）" if data["origin"] == "instance" else "导入的 API（api）")
        if data["aliases"]:
            out.kv("别名", " / ".join(data["aliases"]))
        func = data["function"] + ("（人工指定）" if data["function_source"] == "manual" else "（自动）")
        out.kv("功能", f"{func}  [dim]{data['function_evidence'] or ''}[/dim]")
        out.kv("状态", data["state"])
        for reason in data["state_reasons"]:
            out.line(f"    [dim]· {reason}[/dim]")

        source = data["source"]
        if isinstance(source, dict):
            out.heading("源文件")
            out.kv("路径", str(source.get("path")))
            out.kv("相对路径", str(source.get("rel_path")))
            out.kv("sha256", str(source.get("sha256"))[:16] + "…")
            out.kv("大小/时间", f"{source.get('size')} B · {source.get('mtime')}")

        out.heading("API 图")
        if data["api_sha256"]:
            out.kv("sha256", f"{data['api_sha256'][:16]}…（{data['api_origin']}）")
            out.kv("转换时间", str(data["api_at"]))
            out.kv("已确认", "是" if data["verified"] else "否（首次 run 会拦一下）")
            if data["api_stale"]:
                out.line("    [yellow]! 源文件已变，api.json 是旧的：cwf convert 重新转换[/yellow]")
        else:
            out.line("  还没有 api.json（状态 registered）：cwf convert <ID>")

        nodes = data["nodes"] or {}
        out.heading("节点")
        out.kv(
            "概要",
            f"{nodes.get('total', 0)} 个节点（旁路 {nodes.get('bypassed', 0)}、静音 {nodes.get('muted', 0)}）",
        )

        out.heading(f"已绑参数（{len(data['params'])}）")
        if not data["params"]:
            out.line("  还没有参数。先 cwf inspect <ID> --suggestion 看能绑什么，再 cwf param add。")
        else:
            table = Table(box=None, pad_edge=False, header_style="bold")
            for col in ("名称", "类型", "分类", "默认值", "语义键"):
                table.add_column(col)
            for p in data["params"]:
                default = p["default"]
                if isinstance(default, str) and len(default) > 24:
                    default = default[:24] + "…"
                key = str(p["key"]) + (" [red](orphan)[/red]" if p["orphan"] else "")
                table.add_row(
                    str(p["name"]), str(p["type"]), str(p["category"]), str(default), key
                )
            out.console.print(table)

        if data["warnings"]:
            out.heading("转换警告")
            for w in data["warnings"]:
                out.line(f"  [yellow]![/yellow] {w}")

        presets = data["presets"]
        if presets:
            out.heading(f"预设（{len(presets)}）")
            for name, body in presets.items():
                vals = (body or {}).get("params") or {}
                detail_ = ", ".join(f"{k}={v}" for k, v in vals.items())
                out.line(f"  {name}  [dim]{detail_}[/dim]")
            out.line("  [dim]用 cwf run <ID> --preset <名字>[/dim]")

        out.heading("最近运行")
        if not data["runs"]:
            out.line("  还没跑过。")
        else:
            for r in data["runs"]:
                dur = r["duration_seconds"]
                out.line(
                    f"  {r['run_id']}  [dim]{r['status']} · {str(r['at'])[:19]}"
                    f" · {f'{dur:.1f}s' if isinstance(dur, (int, float)) else '—'}"
                    f" · {r['artifacts']} 个产物[/dim]"
                )

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("show")
    def show_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="ID / 别名 / 名字 / 唯一前缀"),
    ) -> None:
        """看一个条目的全部元信息（只读台账）。"""
        run(ctx.obj, target)
