"""`cwf status`（PRD §7 FR-1 / 附录 §7.3）。离线是正常状态，退出码 0。"""

from __future__ import annotations

from pathlib import Path

import typer

from .. import browser
from ..context import Ctx
from ..output import payload
from ..client import ComfyNotComfyUI, fetch_queue_depth, fetch_stats
from ..config import count_json_files, load
from ..errors import not_initialized
from ..ledger import iter_entries, origin_counts, state_counts


def _gib(value: int | None) -> str:
    if value is None:
        return "未知"
    return f"{value / (1024 ** 3):.1f} GiB"


def run(ctx: Ctx) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    cfg = load(ws)

    warnings: list[str] = []
    online = False
    version: str | None = None
    vram: int | None = None
    try:
        stats = fetch_stats(cfg)
        online = stats.online
        version = stats.version
        vram = stats.vram_total
    except ComfyNotComfyUI:
        warnings.append(f"{cfg.base_url} 有响应但不是 ComfyUI")

    running = pending = None
    if online:
        running, pending = fetch_queue_depth(cfg)

    wf_dir = Path(cfg.instance.workflows_dir) if cfg.instance.workflows_dir else None
    files = count_json_files(wf_dir)

    entries = iter_entries(ws)
    states = state_counts(entries)
    origins = origin_counts(entries)
    ledger_total = len(entries)
    params_total = sum(len(e.params) for e in entries)

    ready = browser.browser_ready()
    if ready is False:
        warnings.append("缺 Playwright 浏览器：python -m playwright install chromium")

    data = payload(
        online=online,
        comfyui_version=version,
        vram_total=vram,
        queue_running=running,
        queue_pending=pending,
        instance_workflow_files=files,
        ledger_entries=ledger_total,
        origins=origins,
        states=states,
        params_total=params_total,
        browser_ready=ready,
        warnings=warnings,
    )

    def human() -> None:
        out = ctx.out
        out.heading("实例")
        if online:
            out.kv("在线", f"{cfg.base_url}（ComfyUI {version or '版本未知'}）", ok=True)
            out.kv("显存", _gib(vram))
            depth = "未知" if running is None else f"运行 {running} / 排队 {pending}"
            out.kv("队列", depth)
        else:
            out.kv("离线", cfg.base_url, ok=False)
            out.line("  本地命令（list / show / inspect）照常可用")

        out.heading("工作流")
        if wf_dir is None:
            out.kv("实例目录", "未配置（先跑 cwf init）", ok=False)
        else:
            out.kv("实例目录", f"{wf_dir}（{files if files is not None else '未探测'} 个 .json）")
        origin_note = f"instance {origins['instance']} + api {origins['api']}"
        out.kv("台账", f"{ledger_total} 条（{origin_note}）")
        out.kv(
            "状态",
            " / ".join(f"{k} {v}" for k, v in states.items() if v) or "还没有条目",
        )
        out.kv("已绑参数", f"{params_total} 个")

        out.heading("环境")
        out.kv("工作区", str(ws.root))
        out.kv("浏览器", browser.describe(ready), ok=bool(ready) if ready is not None else None)

        # ---- 差异提示 ----
        hints: list[str] = []
        if files is not None:
            if files > ledger_total:
                hints.append(f"有 {files - ledger_total} 个文件还没登记，跑 cwf sync")
            elif ledger_total > files + origins["api"]:
                hints.append("台账里有 missing 条目（源文件不在了），可 cwf remove <ID>")
        if ledger_total == 0 and not files:
            hints.append("台账是空的，跑 cwf sync 把实例目录收进来")
        if hints and not ctx.out.quiet:
            out.line("")
            for h in hints:
                out.line(f"[yellow]→[/yellow] {h}")
        for w in warnings:
            out.line(f"[yellow]![/yellow] {w}")

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("status")
    def status_cmd(ctx: typer.Context) -> None:
        """一眼看全：实例、台账、参数、环境（离线可用）。"""
        run(ctx.obj)
