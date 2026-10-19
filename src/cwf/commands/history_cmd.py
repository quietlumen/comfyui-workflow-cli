"""`cwf history` / `cwf log`（PRD §7 FR-6）：读运行记录。"""

from __future__ import annotations

import typer
from rich.table import Table

from .. import logstore
from ..context import Ctx
from ..errors import CwfError, ExitCode, not_initialized
from ..ledger import iter_entries
from ..output import payload
from ..resolve import resolve


def run_history(ctx: Ctx, target: str | None, *, limit: int, status: str | None) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))

    if target:
        entry = resolve(iter_entries(ws), target)
        items = logstore.records(ws, entry.id)
        scope = entry.id
    else:
        items = []
        for e in iter_entries(ws):
            items.extend(logstore.records(ws, e.id))
        items.sort(key=lambda r: str(r.get("run_id")), reverse=True)
        scope = "*"

    if status:
        items = [r for r in items if r.get("status") == status]
    total = len(items)
    items = items[:limit]

    rows = [
        {
            "run_id": r.get("run_id"),
            "workflow_id": r.get("workflow_id"),
            "status": r.get("status"),
            "at": r.get("finished_at") or r.get("started_at") or r.get("queued_at"),
            "duration_seconds": r.get("duration_seconds"),
            "artifacts": len(r.get("artifacts") or []),
        }
        for r in items
    ]
    data = payload(count=total, scope=scope, items=rows)

    def human() -> None:
        if not rows:
            ctx.out.line("还没有运行记录。")
            return
        table = Table(box=None, pad_edge=False, header_style="bold")
        for col in ("run_id", "工作流", "状态", "时间", "耗时", "产物"):
            table.add_column(col)
        for r in rows:
            dur = r["duration_seconds"]
            table.add_row(
                str(r["run_id"]), str(r["workflow_id"]), str(r["status"]),
                str(r["at"]), f"{dur:.1f}s" if isinstance(dur, (int, float)) else "-",
                str(r["artifacts"]),
            )
        ctx.out.console.print(table)
        ctx.out.line(f"{total} 条运行记录（{scope}）。cwf log <run_id> 看单次详情。")

    ctx.out.emit(data, human)


def run_log(ctx: Ctx, run_id: str) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    found = logstore.find_by_run_id(ws, run_id)
    if found is None:
        raise CwfError(
            f"找不到运行记录：{run_id}",
            code=ExitCode.USAGE,
            error_code="NOT_FOUND",
            hint="cwf history 看有哪些 run_id",
        )
    entry_id, record, duplicates = found
    record = _backfill(ctx, entry_id, record)
    body = {k: v for k, v in record.items() if k != "ok"}
    body["workflow_id"] = entry_id
    if duplicates:
        body["duplicate_in"] = duplicates

    def human() -> None:
        # 先给一行速览，再看完整 JSON：翻记录时通常只关心"成没成、多久、几个产物"
        ctx.out.line(f"[bold]{run_id}[/bold] · {logstore.summary(record)}")
        ctx.out.line("")
        ctx.out.line(logstore.as_json(record))
        if duplicates:
            ctx.out.line(
                f"[yellow]![/yellow] 同一个 run_id 还在 {', '.join(duplicates)} 里"
                f"（手工拷贝导致？）——这里显示的是 {entry_id} 的那份"
            )

    ctx.out.emit(payload(**body), human)


def _backfill(ctx: Ctx, entry_id: str, record: dict) -> dict:
    """`--no-wait` 留下的 `status="running"` 记录：能补就就地补全（附录 §6）。

    只在 `log` 里做，不放在 `history` 里——`history` 要快，而且一次列 20 条记录
    全都去轮询会把"看一眼"变成"等一分钟"。
    """
    if record.get("status") != "running" or not record.get("prompt_id"):
        return record
    from ..config import load
    from ..executor import complete_no_wait
    from ..ledger import iter_entries

    entry = next((e for e in iter_entries(ctx.ws) if e.id == entry_id), None)
    if entry is None:
        return record
    try:
        result = complete_no_wait(ctx, load(ctx.ws), entry, str(record["run_id"]))
    except CwfError as exc:
        # 离线 / 超时 / 执行失败都不要把"看记录"这一步也搞失败，只提示一句
        record = dict(record)
        record["backfill_error"] = exc.message
        return record
    return result.record


def register(app: typer.Typer) -> None:
    @app.command("history")
    def history_cmd(
        ctx: typer.Context,
        target: str | None = typer.Argument(None, help="只看这个工作流"),
        limit: int = typer.Option(20, "--limit", help="最多显示几条"),
        status: str | None = typer.Option(None, "--status", help="只看某种状态"),
    ) -> None:
        """看跑过的调用。"""
        run_history(ctx.obj, target, limit=limit, status=status)

    @app.command("log")
    def log_cmd(
        ctx: typer.Context,
        run_id: str = typer.Argument(..., help="run_id（history 里能看到）"),
    ) -> None:
        """看单次运行的完整记录。"""
        run_log(ctx.obj, run_id)
