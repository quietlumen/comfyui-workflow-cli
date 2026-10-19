"""`cwf param`（PRD §7）：只管用户自己决定的绑定 = CRUD。"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..config import load
from ..context import Ctx
from ..errors import not_initialized, usage
from .. import objects
from ..io_utils import atomic_write_json
from ..ledger import Entry, iter_entries
from ..library import _now
from ..modes import load_canonical
from ..output import payload
from ..parameters import (
    match_user_rule,
    candidates,
    is_link,
    load_user_rules,
    make_key,
    semantic_ordinals,
    split_key,
    type_drift,
)
from ..resolve import resolve

CATEGORIES = ("basic", "advanced", "expert")
TYPES = ("string", "integer", "number", "boolean", "combo", "image", "file")


def infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "string"


def _load(ws, target: str) -> tuple[Entry, dict[str, Any]]:
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    entry = resolve(iter_entries(ws), target)
    # 有 api.full.json 就用它：旁路节点上的输入也在这儿，序号也才稳定
    return entry, load_canonical(entry)


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def _looks_like_image_list(options: Any) -> bool:
    """`LoadImage.image` 在 object_info 里是 COMBO，选项是 input 目录里的图片文件名。"""
    if not isinstance(options, list) or not options:
        return False
    sample = [str(o).lower() for o in options[:8]]
    return all(o.endswith(IMAGE_SUFFIXES) for o in sample)


def _save(entry: Entry, params: list[dict[str, Any]]) -> None:
    atomic_write_json(entry.dir / "params.json", params)
    entry.params = params


def _resolve_target_input(
    api: dict[str, Any], *, key: str | None, node: str | None, input_: str | None
) -> tuple[str, str, str, Any]:
    """返回 (key, node_id, input_name, 当前值)。"""
    cands = {c["key"]: c for c in candidates(api)}
    if key:
        if key not in cands:
            raise usage(
                f"没有这个候选输入：{key}",
                "cwf inspect <ID> --suggestion 看有哪些可绑的 key",
            )
        cand = cands[key]
        return key, str(cand["node"]), str(cand["input"]), cand["value"]

    if not node or not input_:
        raise usage("要给 --key \"Class#input#0\"，或 --node N --input I")

    table = semantic_ordinals(api)
    hits = [k for k in cands if cands[k]["node"] == str(node) and cands[k]["input"] == input_]
    if not hits:
        node_obj = api.get(str(node))
        if isinstance(node_obj, dict):
            value = (node_obj.get("inputs") or {}).get(input_)
            if is_link(value):
                raise usage(
                    f"{node}.{input_} 是连线输入，值由别的节点决定，不能绑参数",
                    "换一个值输入；cwf inspect <ID> --nodes 能看到哪些是连线",
                )
        raise usage(
            f"节点 {node} 没有可绑的输入 {input_}",
            "cwf inspect <ID> --nodes 看这个节点有哪些输入",
        )
    cand = cands[hits[0]]
    class_type = str(cand["class_type"])
    ordinal = table[(class_type, input_)].index(str(node))
    return make_key(class_type, input_, ordinal), str(cand["node"]), input_, cand["value"]


def add(
    ctx: Ctx,
    target: str,
    name: str,
    *,
    node: str | None,
    input_: str | None,
    key: str | None,
    label: str | None,
    type_: str | None,
    category: str | None,
    default: Any,
    aliases: list[str],
) -> None:
    ws = ctx.ws
    entry, api = _load(ws, target)
    if not api:
        raise usage(f"{entry.id} 还没有 api.json", f"先 cwf convert {entry.id}")
    if node and input_ and key:
        ctx.out.line("[yellow]![/yellow] 同时给了 --key 和 --node/--input，以 --key 为准")
    if key and (node or input_):
        node = input_ = None

    if any(p.get("name") == name for p in entry.params):
        raise usage(
            f"参数名已存在：{name}",
            f"改名，或 cwf param set {entry.id} {name} --default ...",
        )

    final_key, node_id, input_name, value = _resolve_target_input(
        api, key=key, node=node, input_=input_
    )
    for p in entry.params:
        if p.get("key") == final_key:
            raise usage(
                f"这个输入已经被参数 {p.get('name')} 占用了（{final_key}）",
                "一次输入只能绑一个参数",
            )

    if category is not None and category not in CATEGORIES:
        raise usage(f"category 只能是 {' / '.join(CATEGORIES)}", f"收到：{category}")

    # 用户规则（`rules/params.json`）在这里也生效：没显式给 label/category 就按规则补，
    # 这样 `inspect --suggestion` 推荐的写法和 `param add` 落库的写法不会两套。
    class_type = str(api[node_id]["class_type"])
    rule = match_user_rule(load_user_rules(ws.rules_file), class_type, input_name) or {}
    if label is None:
        label = rule.get("label") or name
    if category is None:
        category = rule.get("category") or "basic"

    # 从 object_info 校正类型/选项/范围：从值猜类型会猜错（megapixels 声明是 FLOAT）
    spec = objects.lookup(ws, load(ws), class_type, input_name) or {}
    declared_type = spec.get("type")
    options = spec.get("options")
    lo, hi = spec.get("min"), spec.get("max")
    final_type = type_ or declared_type or infer_type(value)
    if type_ and declared_type and type_ != declared_type:
        ctx.out.line(
            f"[yellow]![/yellow] {input_name} 在节点里声明的类型是 {declared_type}，你指定了 {type_}"
        )
    # 图片输入在节点里是 COMBO（选项=input 目录里的文件名），但用户要的是"给我一张图"
    if final_type == "combo" and _looks_like_image_list(options):
        final_type = "image"
    if final_type == "combo" and isinstance(options, list):
        chosen = default if default is not None else value
        if chosen not in options:
            raise usage(
                f"{input_name} 的默认值 {chosen!r} 不在可选值里",
                f"可选：{', '.join(map(str, options))}",
            )

    param = {
        "name": name,
        "label": label or name,
        "aliases": list(aliases),
        "type": final_type,
        "category": category or "basic",        "key": final_key,
        "node": node_id,
        "input": input_name,
        "default": default if default is not None else value,
        "options": options,
        "min": lo,
        "max": hi,
        "created_at": _now(),
        "orphan": False,
    }
    params = list(entry.params) + [param]
    _save(entry, params)
    _emit(ctx, entry, param, action="added")


def set_(
    ctx: Ctx,
    target: str,
    name: str,
    *,
    label: str | None,
    category: str | None,
    default: Any,
    aliases: list[str] | None,
    type_: str | None = None,
) -> None:
    ws = ctx.ws
    entry, _api = _load(ws, target)
    found = None
    for p in entry.params:
        if p.get("name") == name:
            found = p
            break
    if found is None:
        raise usage(f"没有这个参数：{name}", f"cwf param list {entry.id}")
    if category is not None and category not in CATEGORIES:
        raise usage(f"category 只能是 {' / '.join(CATEGORIES)}", f"收到：{category}")
    if type_ is not None and type_ not in TYPES:
        raise usage(f"--type 只能是 {' / '.join(TYPES)}", f"收到：{type_}")

    changes = []
    if label is not None:
        found["label"] = label
        changes.append("label")
    if category is not None:
        found["category"] = category
        changes.append("category")
    if type_ is not None and type_ != found.get("type"):
        found["type"] = type_
        # 只改类型不够：`coerce` 的校验靠 options/min/max，这三样也得跟着这个类型走。
        # 缓存里查不到就保留原样（离线时不能凭空清掉范围）。
        try:
            class_type, input_name, _ord = split_key(str(found.get("key") or ""))
        except (ValueError, IndexError):
            class_type, input_name = "", ""
        spec = objects.lookup(ws, load(ws), class_type, input_name) or {}
        if spec:
            found["type"] = spec.get("type") or type_
            found["options"] = spec.get("options")
            found["min"] = spec.get("min")
            found["max"] = spec.get("max")
        changes.append("type")
    if default is not None:
        found["default"] = default
        changes.append("default")
    if aliases is not None:
        found["aliases"] = [a for a in aliases if a]
        changes.append("aliases")
    if changes:
        _save(entry, list(entry.params))
    data = payload(id=entry.id, name=name, changed=changes, param=found)

    def human() -> None:
        if changes:
            ctx.out.line(f"[green]✓[/green] {name} 已更新（{', '.join(changes)}）")
        else:
            ctx.out.line(f"{name} 没有变化（幂等）。")

    ctx.out.emit(data, human)


def rm(ctx: Ctx, target: str, name: str | None, *, all_: bool) -> None:
    ws = ctx.ws
    entry, _api = _load(ws, target)
    if all_:
        removed = [p.get("name") for p in entry.params]
        _save(entry, [])
    else:
        if not name:
            raise usage("要给参数名，或 --all")
        removed = [p.get("name") for p in entry.params if p.get("name") == name]
        if not removed:
            raise usage(f"没有这个参数：{name}", f"cwf param list {entry.id}")
        _save(entry, [p for p in entry.params if p.get("name") != name])

    data = payload(id=entry.id, removed=removed)

    def human() -> None:
        ctx.out.line(f"[green]✓[/green] 已移除 {len(removed)} 个参数：{', '.join(map(str, removed))}")
        ctx.out.line("  api.json 没动；只是接口少了几项。")

    ctx.out.emit(data, human)


def list_(ctx: Ctx, target: str) -> None:
    ws = ctx.ws
    entry, _api = _load(ws, target)
    drift = type_drift(entry.params, objects.load(ws))
    data = payload(
        id=entry.id, name=entry.name, count=len(entry.params),
        params=entry.params, type_drift=drift,
    )

    def human() -> None:
        out = ctx.out
        if not entry.params:
            out.line(f"{entry.id} 还没有绑定任何参数。")
            out.line(f"  先看能绑什么：cwf inspect {entry.id} --suggestion")
            return
        table = Table(box=None, pad_edge=False, header_style="bold")
        for col in ("名称", "标签", "类型", "分类", "默认值", "别名", "语义键"):
            table.add_column(col)
        for p in entry.params:
            table.add_row(
                str(p.get("name")),
                str(p.get("label")),
                str(p.get("type")),
                str(p.get("category")),
                str(p.get("default"))[:24],
                "/".join(p.get("aliases") or []) or "-",
                str(p.get("key")) + (" [red](orphan)[/red]" if p.get("orphan") else ""),
            )
        out.console.print(table)
        for d in drift:
            out.line(
                f"  [yellow]![/yellow] {d['name']}：记录的是 {d['stored']}，"
                f"{d['key']} 在节点里声明的是 {d['raw']}"
            )
        if drift:
            fix = "  ".join(
                f"cwf param set {entry.id} {d['name']} --type {d['declared']}" for d in drift
            )
            out.line(f"  [dim]按节点声明修正：{fix}[/dim]")
        out.line(f"{len(entry.params)} 个参数（{entry.id}）。这就是 {entry.name} 的用户接口。")

    ctx.out.emit(data, human)


def _emit(ctx: Ctx, entry: Entry, param: dict[str, Any], *, action: str) -> None:
    data = payload(id=entry.id, action=action, param=param, count=len(entry.params))

    def human() -> None:
        out = ctx.out
        out.line(f"[green]✓[/green] 已绑定 {param['name']} → {param['key']}")
        out.line(f"  类型 {param['type']} · 默认值 {param['default']!r} · 分类 {param['category']}")
        out.line(f"  {entry.name} 现在有 {len(entry.params)} 个参数：cwf param list {entry.id}")

    ctx.out.emit(data, human)


param_app = typer.Typer(add_completion=False, help="参数绑定（只记你的决定）")


@param_app.command("list")
def _cmd_list(ctx: typer.Context, target: str = typer.Argument(...)) -> None:
    """列出已绑定的参数。"""
    list_(ctx.obj, target)


@param_app.command("add")
def _cmd_add(
    ctx: typer.Context,
    target: str = typer.Argument(...),
    name: str = typer.Argument(..., help="参数名（你的接口里叫它什么）"),
    node: str | None = typer.Option(None, "--node", help="节点 ID"),
    input_: str | None = typer.Option(None, "--input", help="输入名"),
    key: str | None = typer.Option(None, "--key", help="语义键 Class#input#0"),
    label: str | None = typer.Option(None, "--label"),
    type_: str | None = typer.Option(None, "--type"),
    category: str | None = typer.Option(None, "--category"),
    default: str | None = typer.Option(None, "--default"),
    aliases: list[str] = typer.Option([], "--alias"),
) -> None:
    """把你选中的一个输入绑定成参数。"""
    add(
        ctx.obj, target, name,
        node=node, input_=input_, key=key, label=label, type_=type_,
        category=category, default=default, aliases=list(aliases),
    )


@param_app.command("set")
def _cmd_set(
    ctx: typer.Context,
    target: str = typer.Argument(...),
    name: str = typer.Argument(...),
    label: str | None = typer.Option(None, "--label"),
    category: str | None = typer.Option(None, "--category"),
    default: str | None = typer.Option(None, "--default"),
    aliases: list[str] = typer.Option([], "--alias"),
    type_: str | None = typer.Option(None, "--type", help="修正类型（配合 param list 的漂移提示）"),
) -> None:
    """改参数的展示信息、默认值与类型（不动绑定目标）。"""
    set_(
        ctx.obj, target, name,
        label=label, category=category, default=default,
        aliases=list(aliases) if aliases else None,
        type_=type_,
    )


@param_app.command("rm")
def _cmd_rm(
    ctx: typer.Context,
    target: str = typer.Argument(...),
    name: str | None = typer.Argument(None),
    all_: bool = typer.Option(False, "--all"),
) -> None:
    """移除绑定（不影响 api.json）。"""
    rm(ctx.obj, target, name, all_=all_)


def register(app: typer.Typer) -> None:
    app.add_typer(param_app, name="param")
