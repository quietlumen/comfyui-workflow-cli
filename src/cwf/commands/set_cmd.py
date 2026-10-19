"""`cwf set`（PRD §7）：改元信息里的"人给的那部分"。"""

from __future__ import annotations

import typer

from ..context import Ctx
from ..errors import not_initialized, usage
from ..graph import classify_function
from ..io_utils import atomic_write_json, read_json_or
from ..ledger import iter_entries
from ..output import payload
from ..resolve import resolve

FUNCTIONS = ("text_to_image", "image_edit", "upscale", "video", "other")


def _current_graph(entry) -> dict | None:
    """优先用源文件；api 条目没有源文件，就用 api.json 推分类。"""
    for path in (entry.source_file, entry.api_file):
        raw = read_json_or(path, None)
        if isinstance(raw, dict):
            return raw
    return None


def run(
    ctx: Ctx,
    target: str,
    *,
    function: str | None,
    aliases: list[str] | None,
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    entry = resolve(iter_entries(ws), target)

    changes: list[str] = []
    meta = dict(entry.meta)

    if function is not None:
        if function == "auto":
            raw = _current_graph(entry)
            if raw is None:
                raise usage(
                    "没有可用的图来判断分类（源文件与 api.json 都不在）",
                    "先 cwf convert <ID>，或直接指定 --function <值>",
                )
            fn, evidence = classify_function(raw)
            if meta.get("function") != fn or meta.get("function_source") != "auto":
                meta["function"] = fn
                meta["function_evidence"] = evidence
                meta["function_source"] = "auto"
                changes.append(f"function → {fn}（自动：{evidence}）")
        else:
            if function not in FUNCTIONS:
                raise usage(
                    f"function 只能是 {' / '.join(FUNCTIONS)} 或 auto",
                    f"收到：{function}",
                )
            if meta.get("function") != function or meta.get("function_source") != "manual":
                meta["function"] = function
                meta["function_evidence"] = "人工指定"
                meta["function_source"] = "manual"
                changes.append(f"function → {function}（人工，sync 不再覆盖）")

    if aliases is not None:
        cleaned = [a for a in aliases if a != ""]
        if (meta.get("aliases") or []) != cleaned:
            meta["aliases"] = cleaned
            changes.append("别名 → " + (" / ".join(cleaned) if cleaned else "（清空）"))

    if changes:
        from ..library import _now  # 复用同一套时间格式

        meta["updated_at"] = _now()
        atomic_write_json(entry.dir / "meta.json", meta)

    data = payload(
        id=entry.id,
        name=entry.name,
        changed=bool(changes),
        changes=changes,
        function=meta.get("function"),
        function_source=meta.get("function_source"),
        function_evidence=meta.get("function_evidence"),
        aliases=meta.get("aliases") or [],
    )

    def human() -> None:
        out = ctx.out
        if not changes:
            out.line(f"{entry.id} 没有变化（幂等）。")
            return
        for c in changes:
            out.line(f"[green]✓[/green] {c}")
        out.line(f"[dim]{entry.id} 已更新[/dim]")

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("set")
    def set_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="ID / 别名 / 名字 / 唯一前缀"),
        function: str | None = typer.Option(
            None, "--function", help=f"{' / '.join(FUNCTIONS)} / auto"
        ),
        alias: list[str] = typer.Option([], "--alias", help="别名（可重复；给空串清空）"),
    ) -> None:
        """改条目的功能分类或别名（只写 meta.json）。"""
        run(
            ctx.obj,
            target,
            function=function,
            aliases=list(alias) if alias else None,
        )
