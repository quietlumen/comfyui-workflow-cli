"""`cwf inspect`（PRD §7）：整张图的完整信息，四段，全部只读。

数据只来自台账里的 `api.json`（有 source.json 也不解析 UI，--nodes 走 API 格式）。
"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..context import Ctx
from ..errors import not_initialized, usage
from ..ledger import iter_entries
from ..modes import load_canonical, load_full, skipped_ids
from ..objects import load as load_objects
from ..parameters import (
    _raw_declared,
    is_link,
    load_user_rules,
    plan_relocate,
    semantic_ordinals,
    suggest,
    type_drift,
)
from ..resolve import resolve

SECTIONS = ("nodes", "graph", "suggestion", "warnings")


def _declared(specs: dict[str, Any], class_type: str, input_name: str) -> str | None:
    """`object_info` 里那个输入的**原始**类型名（`INT` / `MODEL` / `COMBO`…）。

    与 `parameters.input_spec()` 的区别：那个给 cwf 的语义类型（`integer`/`file`…），
    这里给节点声明的原文——`--nodes` 是"逐输入明细"，要的是后者。
    """
    return _raw_declared(specs, class_type, input_name)


def _nodes(
    api: dict[str, Any],
    skipped: set[str] | None = None,
    *,
    specs: dict[str, Any] | None = None,
    bound: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    skipped = skipped or set()
    specs = specs or {}
    bound = bound or {}
    table = semantic_ordinals(api)
    out: list[dict[str, Any]] = []
    for node_id, node in sorted(
        ((str(k), v) for k, v in api.items() if isinstance(v, dict)),
        key=lambda kv: (int(kv[0]) if kv[0].isdigit() else 10**9, kv[0]),
    ):
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        rows = []
        for name, value in inputs.items():
            linked = is_link(value)
            ordinal = table[(class_type, str(name))].index(node_id)
            key = f"{class_type}#{name}#{ordinal}"
            rows.append(
                {
                    "name": str(name),
                    "type": _declared(specs, class_type, str(name)),
                    "value": None if linked else value,
                    "link": f"{value[0]}.{value[1]}" if linked else None,
                    "candidate": not linked,
                    "bound": bound.get(key),
                    "key": key,
                }
            )
        out.append(
            {
                "id": node_id,
                "class_type": class_type,
                "title": node.get("_meta", {}).get("title") if isinstance(node.get("_meta"), dict) else None,
                "bypassed": node_id in skipped,
                "muted": False,
                "inputs": rows,
            }
        )
    return out


def _graph(api: dict[str, Any], *, specs: dict[str, Any] | None = None) -> dict[str, Any]:
    specs = specs or {}
    links: list[dict[str, str]] = []
    for node_id, node in api.items():
        if not isinstance(node, dict):
            continue
        for name, value in (node.get("inputs") or {}).items():
            if is_link(value):
                src = api.get(str(value[0])) or {}
                links.append(
                    {
                        "from": f"{value[0]}.{value[1]}",
                        "to": f"{node_id}.inputs.{name}",
                        # 连线的类型 = 源节点那个**输出槽**声明的类型
                        "type": _output_type(specs, str(src.get("class_type") or ""), int(value[1])),
                    }
                )
    return {"count": len(links), "links": links}


def _output_type(specs: dict[str, Any], class_type: str, slot: int) -> str | None:
    node = specs.get(class_type)
    if not isinstance(node, dict):
        return None
    outputs = node.get("output")
    if isinstance(outputs, list) and 0 <= slot < len(outputs):
        return str(outputs[slot]) if outputs[slot] else None
    return None


def _suggestion(
    api: dict[str, Any], params: list[dict[str, Any]], rules: list[dict], skipped: set[str] | None = None
) -> dict[str, Any]:
    return suggest(api, params, rules, skipped=skipped)


def _unknown_types(nodes: list[dict], specs: dict[str, Any]) -> list[str]:
    """`object_info` 缓存里查不到的节点类型（排好序、去重）。

    没有缓存时返回空列表——"没查过"和"查了没有"是两件事，不该混成一片警告。
    """
    if not specs:
        return []
    seen = {
        str(n["class_type"])
        for n in nodes
        if n["class_type"] and str(n["class_type"]) not in specs
    }
    return sorted(seen)


def _summary(
    nodes: list[dict],
    graph: dict,
    sugg: dict,
    params: list,
    *,
    warnings: list | None = None,
    unknown: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": len(nodes),
        "links": graph["count"],
        "bypassed": sum(1 for n in nodes if n["bypassed"]),
        "muted": sum(1 for n in nodes if n["muted"]),
        "unknown_types": list(unknown or []),
        "params": len(params),
        "candidates": len(sugg["params"]),
        "warnings": len(warnings or []),
    }


def run(
    ctx: Ctx,
    target: str,
    *,
    show_nodes: bool,
    show_graph: bool,
    show_suggestion: bool,
    show_warnings: bool,
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    entry = resolve(iter_entries(ws), target)
    # 有 api.full.json 就用完整图：旁路节点也在这儿，否则"靠 skip 控制的输入"根本看不见
    api = load_canonical(entry)
    if not isinstance(api, dict) or not api:
        raise usage(
            f"{entry.id} 还没有 api.json（状态 {entry.state}）",
            f"先 cwf convert {entry.id}",
        )
    skipped = skipped_ids(entry.meta)
    if api is not None and skipped:
        skipped = skipped & set(api)

    # 类型与"已绑给谁"都来自本地缓存（`cache/object_info.json`）与台账，
    # 不连实例、不刷新缓存——`inspect` 是只读命令，冷启动也不能被网络拖住（N10）。
    specs = load_objects(ws)
    bound = {str(p.get("key")): str(p.get("name")) for p in entry.params if p.get("key")}
    nodes = _nodes(api, skipped, specs=specs, bound=bound)
    graph = _graph(api, specs=specs)
    sugg = _suggestion(api, entry.params, load_user_rules(ws.rules_file), skipped)
    warnings = entry.meta.get("warnings") or []
    unknown = _unknown_types(nodes, specs)
    # 两件"会悄悄坏掉"的事，读的时候顺手算一遍（都不写盘）：
    # 节点 ID 漂了（语义键还能定位到别的 ID）、参数类型与节点声明不一致。
    node_drift = plan_relocate(entry.params, api)
    if not load_full(entry):
        # 没有 api.full.json 时，api.json 是"照文件旁路状态"的部分图：
        # 被旁路的节点不在里面，绑在它们上面的参数定位不到**不算漂移**——
        # 那是"这张图上没有这个节点"，不是"它的 ID 变了"（要完整图才有依据）。
        # 参数记的节点 ID 落在源文件的旁路集合里 → 过滤掉这个 `?`，避免误报。
        raw_skipped = skipped_ids(entry.meta)
        node_drift = [
            d for d in node_drift
            if not (d.get("to") == "?" and d.get("from") in raw_skipped)
        ]
    type_drift_list = type_drift(entry.params, specs)

    picked = [show_nodes, show_graph, show_suggestion, show_warnings]
    want_all = not any(picked)
    summary = _summary(
        nodes, graph, sugg, entry.params, warnings=warnings, unknown=unknown
    )
    summary["bypassed"] = len(skipped)
    summary["full"] = bool(load_full(entry))
    summary["node_drift"] = len(node_drift)
    summary["type_drift"] = len(type_drift_list)
    data: dict[str, Any] = {
        "ok": True,
        "id": entry.id,
        "name": entry.name,
        "state": entry.state,
        "summary": summary,
    }
    if want_all or show_nodes:
        data["node"] = nodes
    if want_all or show_graph:
        data["graph"] = graph
    if want_all or show_suggestion:
        data["suggestion"] = sugg
    if want_all or show_warnings:
        data["warnings"] = warnings
        data["drift"] = {"node": node_drift, "type": type_drift_list}

    def human() -> None:
        out = ctx.out
        s = data["summary"]
        out.heading(
            f"{entry.id}  {entry.name}  [dim]{entry.state} · "
            f"{s['nodes']} 节点 · {s['links']} 连线 · {s['candidates']} 个候选输入 · "
            f"{s['params']} 个参数[/dim]"
        )
        if s["bypassed"]:
            out.line(
                f"  [yellow]旁路 {s['bypassed']} 个节点[/yellow]"
                f"（跑的时候默认不执行；`--enable <ID>` 或给绑在它上面的参数传值就打开）"
            )
            if not s["full"]:
                out.line("  [yellow]![/yellow] 没有 api.full.json：这些节点现在打不开，"
                         f"先 cwf convert {entry.id} --force")
        if s["unknown_types"]:
            out.line(f"  [yellow]![/yellow] {len(s['unknown_types'])} 个节点类型不在 "
                     f"cache/object_info.json 里（类型列留空）："
                     f"{', '.join(s['unknown_types'][:3])}"
                     f"{' …' if len(s['unknown_types']) > 3 else ''}")
        if "node" in data:
            out.heading("节点与输入")
            table = Table(box=None, pad_edge=False, header_style="bold")
            for col in ("节点", "类型", "输入", "输入类型", "当前值 / 来源", "可绑", "已绑"):
                table.add_column(col)
            for n in data["node"]:
                ctype = n["class_type"] + (" [yellow](旁路)[/yellow]" if n["bypassed"] else "")
                for inp in n["inputs"]:
                    where = inp["link"] or json_short(inp["value"])
                    table.add_row(
                        n["id"], ctype, inp["name"], inp["type"] or "",
                        where,
                        "✓" if inp["candidate"] else "",
                        inp["bound"] or "",
                    )
            out.console.print(table)
        if "graph" in data:
            out.heading(f"连线（{data['graph']['count']}）")
            for link in data["graph"]["links"]:
                tail = f"  [dim]{link['type']}[/dim]" if link["type"] else ""
                out.line(f"  {link['from']} → {link['to']}{tail}")
        if "suggestion" in data:
            rows = data["suggestion"]["params"]
            rec = [r for r in rows if r["recommended"]]
            out.heading(f"建议绑定的参数（{len(rec)} 个推荐 / {len(rows)} 个候选）")
            for row in rec:
                mark = "[dim](已绑)[/dim]" if row["already_bound"] else ""
                tag = " [yellow][旁路][/yellow]" if row.get("skipped") else ""
                out.line(
                    f"  [green]{row['suggested_name']:<20}[/green] {row['key']}  "
                    f"[dim]{json_short(row['default'], 28)} · {row['category']} · "
                    f"{row['suggest_source']}[/dim]{tag} {mark}"
                )
            others = len(rows) - len(rec)
            if others:
                out.line(f"  [dim]另有 {others} 个能绑但不建议绑（加载器/技术参数等），"
                         f"--json 里有完整列表[/dim]")
            out.line("")
            out.line("      照着绑：cwf param add <ID> <名字> --key \"<key>\"")
        if "warnings" in data and data["warnings"]:
            out.heading("警告")
            for w in data["warnings"]:
                out.line(f"  [yellow]![/yellow] {w}")
        if "drift" in data:
            drift = data["drift"]
            if drift["node"]:
                out.heading(f"节点 ID 漂移（{len(drift['node'])}）")
                for d in drift["node"]:
                    out.line(f"  {d['name']}：{d['from']} → {d['to']}（语义键还能定位）")
                out.line("  [dim]cwf convert <ID> --force 会把这些写回去[/dim]")
            if drift["type"]:
                out.heading(f"参数类型与节点声明不一致（{len(drift['type'])}）")
                for d in drift["type"]:
                    out.line(f"  {d['name']}：记录 {d['stored']} · 声明 {d['raw']}")
                for d in drift["type"]:
                    out.line(
                        f"  [dim]cwf param set {entry.id} {d['name']} --type {d['declared']}[/dim]"
                    )

    ctx.out.emit(data, human)


def json_short(value: Any, limit: int = 40) -> str:
    if value is None:
        return "-"
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def register(app: typer.Typer) -> None:
    @app.command("inspect")
    def inspect_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="ID / 别名 / 名字 / 唯一前缀"),
        nodes: bool = typer.Option(False, "--nodes", help="只看节点与每个输入"),
        graph: bool = typer.Option(False, "--graph", help="只看连线"),
        suggestion: bool = typer.Option(False, "--suggestion", help="只看建议绑定的参数"),
        warnings: bool = typer.Option(False, "--warnings", help="只看警告"),
    ) -> None:
        """看整张图（默认全部段；带 flag 只看某几段，全部只读）。"""
        run(
            ctx.obj,
            target,
            show_nodes=nodes,
            show_graph=graph,
            show_suggestion=suggestion,
            show_warnings=warnings,
        )
