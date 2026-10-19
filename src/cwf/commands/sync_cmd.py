"""`cwf sync`（PRD §7）：扫目录 → 身份判定 → 登记/更新。"""

from __future__ import annotations

from pathlib import Path

import typer

from ..context import Ctx
from ..config import load
from ..errors import CwfError, ExitCode, not_initialized, usage
from ..ledger import iter_entries
from ..library import SyncReport, adopt, sync
from ..output import payload
from ..resolve import resolve

_MARKS = ("new", "changed", "moved", "moved_changed", "missing", "unchanged")
_MARK_STYLE = {
    "new": "green",
    "changed": "yellow",
    "moved": "cyan",
    "moved_changed": "cyan",
    "missing": "red",
    "unchanged": "dim",
}


def _parse_adopt(pairs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in pairs:
        if "=" not in raw:
            raise usage(f"--adopt 要写成 NEW=OLD，收到：{raw}", "例：cwf sync --adopt wf-0046=wf-0012")
        new, old = raw.split("=", 1)
        out.append((new.strip(), old.strip()))
    return out


def run(
    ctx: Ctx,
    *,
    only: str | None,
    convert: bool,
    dry_run: bool,
    no_move: bool,
    adopt_pairs: list[str],
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    cfg = load(ws)
    if convert and dry_run:
        raise usage("--convert 不能和 --dry-run 一起用", "--dry-run 不落库，也就没有可转换的条目")

    wf_dir = Path(cfg.instance.workflows_dir) if cfg.instance.workflows_dir else None
    if wf_dir is None:
        raise CwfError(
            "配置里没有工作流目录",
            code=ExitCode.USAGE,
            error_code="NO_WORKFLOWS_DIR",
            hint="cwf init --instance-dir <ComfyUI 目录>",
        )

    only_id = resolve(iter_entries(ws), only).id if only else None
    report: SyncReport = sync(
        ws, wf_dir, only=only_id, dry_run=dry_run, no_move=no_move
    )

    adopted: list[str] = []
    if adopt_pairs:
        if dry_run:
            raise usage("--adopt 不能和 --dry-run 一起用")
        for new_id, old_id in _parse_adopt(adopt_pairs):
            adopted.append(adopt(ws, new_id, old_id))

    converted, convert_failed, convert_skipped = (
        _convert_changed(ctx, cfg, report) if convert else ([], [], [])
    )
    if convert:
        _mark_converted(report.changes, converted, convert_failed)

    data = payload(
        **report.to_json(),
        dry_run=dry_run,
        adopted=adopted,
        counts=report.counts,
        converted=converted,
        convert_failed=convert_failed,
        convert_skipped=convert_skipped,
    )

    def human() -> None:
        out = ctx.out
        out.heading(f"扫描 {report.scanned} 个文件" + ("（dry-run，未落库）" if dry_run else ""))
        if not report.changes:
            out.line("  没有变化。")
        for mark in _MARKS:
            rows = [c for c in report.changes if c.mark == mark]
            if not rows:
                continue
            style = _MARK_STYLE[mark]
            out.line(f"  [{style}]{mark:<14}[/{style}] {len(rows)}")
            for c in rows:
                eid = c.id or "-"
                out.line(f"      {eid:<10} {c.rel_path or '-'}  [dim]{c.reason}[/dim]")
        if report.suspects:
            out.line("")
            out.line("[yellow]疑似同一个工作流（没敢自动合并）：[/yellow]")
            for s in report.suspects:
                out.line(
                    f"      {s.rel_path} → 候选 {', '.join(s.candidates)}"
                    f"（相似度 {s.best_j:.2f}）"
                )
                out.line(f"      确认就：cwf sync --adopt {s.new_id}={s.candidates[0]}")
        for msg in adopted:
            out.line(f"[green]✓[/green] {msg}")
        if convert_skipped:
            for why in convert_skipped:
                out.line(f"[yellow]![/yellow] {why}")
        for item in converted:
            out.line(
                f"[green]✓[/green] {item['id']}（{item['name']}）转换完成"
                f"（{item['nodes']} 个节点）"
            )
            for d in item.get("drift") or []:
                out.line(f"  [yellow]参数漂移[/yellow] {d['name']}: 节点 {d['from']} → {d['to']}")
        for f in convert_failed:
            out.line(f"[red]✗[/red] {f['id']} 转换失败：{f['error']}")
        if not dry_run and report.counts.get("new") and not convert:
            out.line("")
            out.line("下一步：cwf list 看台账；转换用 cwf convert <ID>")

    ctx.out.emit(data, human)


_CONVERTIBLE = ("new", "changed", "moved_changed")


def _mark_converted(changes, converted: list[dict], failed: list[dict]) -> None:
    """把"这条到底转没转"写回 `changes[]`（附录 §7.4 的 `converted` 三态）。

    带 `--convert` 却没转的（`unchanged` / `missing` / `duplicate`）是 `False`，
    转失败也是 `False`——失败清单在 `convert_failed` 里单列，这里不重复表达。
    """
    done = {str(c["id"]) for c in converted}
    bad = {str(f["id"]) for f in failed}
    for c in changes:
        if c.mark not in _CONVERTIBLE:
            c.converted = False
        elif c.id in done:
            c.converted = True
        elif c.id in bad:
            c.converted = False
        # 剩下的（例如实例离线整段跳过）保持 None：确实没尝试


def _convert_changed(
    ctx: Ctx, cfg, report: SyncReport
) -> tuple[list[dict], list[dict], list[str]]:
    """`--convert`：把本轮 `new`/`changed`/`moved_changed` 顺手转成 API（附录 §7.4）。

    实例离线就整段跳过并说明原因——**不报错**：`sync` 本身是纯本地扫描，
    不该因为没开 ComfyUI 就失败。
    """
    from ..client import ComfyNotComfyUI, fetch_stats
    from ..commands.convert_cmd import _capture_and_write

    wanted = [c.id for c in report.changes if c.mark in _CONVERTIBLE and c.id]
    if not wanted:
        return [], [], []
    try:
        stats = fetch_stats(cfg)
    except ComfyNotComfyUI:
        return [], [], [f"{cfg.base_url} 有响应但不是 ComfyUI，跳过转换"]
    if not stats.online:
        return [], [], [f"实例离线（{cfg.base_url}），跳过转换；上线后 cwf convert --all"]

    entries = [e for e in iter_entries(ctx.ws) if e.id in set(wanted)]
    if not entries:
        return [], [], []
    converted, failed, _refreshed = _capture_and_write(ctx, cfg, entries, visible=False)
    return converted, failed, []


def register(app: typer.Typer) -> None:
    @app.command("sync")
    def sync_cmd(
        ctx: typer.Context,
        target: str | None = typer.Argument(None, help="只处理这一个条目（ID/名字/前缀）"),
        convert: bool = typer.Option(False, "--convert", help="登记完后接着转 API（实例在线才转）"),
        dry_run: bool = typer.Option(False, "--dry-run", help="只报变更，不落库、不占 ID"),
        no_move: bool = typer.Option(False, "--no-move", help="保守模式：不做指纹猜测"),
        adopt_: list[str] = typer.Option(
            [], "--adopt", help="手工接管：NEW=OLD（可重复）"
        ),
    ) -> None:
        """把实例目录里的工作流收进台账（只读实例，不改它）。"""
        run(
            ctx.obj,
            only=target,
            convert=convert,
            dry_run=dry_run,
            no_move=no_move,
            adopt_pairs=list(adopt_),
        )
