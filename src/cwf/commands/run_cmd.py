"""`cwf run`（PRD §7 FR-3）：一次提交，一条命令。"""

from __future__ import annotations

import typer
from rich.table import Table

from ..client import guard_remote
from ..config import load
from ..context import Ctx
from ..errors import not_initialized, usage
from ..executor import Prepared, RunResult, dry_run_table, prepare, run_once
from ..io_utils import atomic_write_json, read_json_or
from ..ledger import Entry, iter_entries
from ..modes import describe
from ..output import payload
from ..parameters import parse_kv
from ..prompts import gate
from ..resolve import resolve


def _parse_ids(items: list[str], flag: str) -> list[str]:
    """`--enable 470,475` / `--enable 470 --enable 475` 都认。"""
    out: list[str] = []
    for raw in items:
        for part in str(raw).replace(" ", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise usage(f"{flag} 只接节点 ID（数字），收到 {part!r}",
                            "cwf inspect <ID> --nodes 看节点 ID")
            out.append(part)
    return out


def _confirmation(ctx: Ctx, entry: Entry, payload: dict | None = None) -> None:
    """PRD §10 的两道关口（首次未验证 + 高成本）。判定在 `prompts.gate`，三处共用。"""
    gate(ctx, entry, [payload] if payload else [])


def _save_preset(entry: Entry, name: str, values: dict[str, str]) -> None:
    presets = read_json_or(entry.dir / "presets.json", {})
    if not isinstance(presets, dict):
        presets = {}
    presets[name] = {"params": values}
    atomic_write_json(entry.dir / "presets.json", presets)


def run(
    ctx: Ctx,
    target: str,
    *,
    params: list[str],
    sets: list[str],
    presets: list[str],
    save_preset: str | None,
    dry_run: bool,
    out: str | None,
    prefix: str | None,
    random_seed: bool,
    timeout: int | None,
    no_wait: bool,
    allow_remote: bool,
    enable: list[str] | None = None,
    skip: list[str] | None = None,
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    cfg = load(ws)
    guard_remote(cfg, allow=allow_remote, command="run")
    entry = resolve(iter_entries(ws), target)

    cli_params = parse_kv(params)
    enable_ids = _parse_ids(enable or [], "--enable")
    skip_ids = _parse_ids(skip or [], "--skip")

    if save_preset:
        prepared = prepare(entry, cli_params=cli_params, random_seed=random_seed)
        values = {
            k: v for k, v in prepared.values.items() if prepared.sources.get(k) == "cli"
        }
        if not values:
            raise usage(
                f"没给任何 --param，预设 {save_preset} 会是空的",
                "例：cwf run <ID> --save-preset fast --param steps=8",
            )
        _save_preset(entry, save_preset, values)
        data = payload(id=entry.id, preset=save_preset, params=values)
        ctx.out.emit(data, lambda: ctx.out.line(f"[green]✓[/green] 已存预设 {save_preset}：{values}"))
        return

    if dry_run:
        prepared = prepare(
            entry,
            cli_params=cli_params,
            presets=presets,
            overrides=sets,
            random_seed=random_seed,
            prefix=prefix,
            cfg=None,
            enable=enable_ids,
            skip=skip_ids,
        )
        assert isinstance(prepared, Prepared)
        data = payload(
            id=entry.id,
            dry_run=True,
            dry_run_note="不会提交",
            params=prepared.values,
            param_sources=prepared.sources,
            overrides=prepared.overrides,
            orphans=prepared.orphans,
            mode=prepared.mode,
            uploads=prepared.uploads,
            payload=prepared.payload,
        )

        def human() -> None:
            out_ = ctx.out
            out_.heading(f"{entry.id}  {entry.name}  [dim]（dry-run，不会提交）[/dim]")
            table = Table(box=None, pad_edge=False, header_style="bold")
            for col in ("参数", "值", "来源"):
                table.add_column(col)
            for name, value, source in dry_run_table(prepared, entry):
                table.add_row(name, value, source)
            if not entry.params and not prepared.overrides:
                out_.line("  这个流还没绑参数，payload 就是 api.json 原样。")
            else:
                out_.console.print(table)
            for line in describe(prepared.mode):
                out_.line(f"  [cyan]模式[/cyan] {line}")
            for up in prepared.uploads:
                state = "会上传" if up.get("uploaded") else "待上传（dry-run）"
                out_.line(f"  [cyan]图片[/cyan] {up['param']}: {up['local_path']} → {state}")
            if prepared.orphans:
                out_.line(f"  [yellow]![/yellow] 定位不到的参数：{', '.join(prepared.orphans)}")
            out_.line("")
            out_.line(f"  最终 payload：{len(prepared.payload)} 个节点（--json 里是完整内容）")

        ctx.out.emit(data, human)
        return

    # 先组包再确认：高成本判定看的是**这次真的会发出去**的 payload（含上传后的文件名）
    prepared = prepare(
        entry,
        cli_params=cli_params,
        presets=presets,
        overrides=sets,
        random_seed=random_seed,
        prefix=prefix,
        cfg=cfg,
        enable=enable_ids,
        skip=skip_ids,
    )
    assert isinstance(prepared, Prepared)
    _confirmation(ctx, entry, prepared.payload)
    result = run_once(
        ctx,
        cfg,
        entry,
        presets=presets,
        out_dir=out,
        timeout=timeout,
        wait=not no_wait,
        client_id="cwf",
        prepared=prepared,
    )
    assert isinstance(result, RunResult)
    data = payload(
        run_id=result.run_id,
        prompt_id=result.prompt_id,
        status=result.status,
        duration_seconds=result.duration_seconds,
        artifacts=result.artifacts,
        params_applied={k: v for k, v in result.record.get("params", {}).items()},
        mode=result.record.get("mode"),
        uploads=result.record.get("uploads"),
        exit_code=0,
    )

    def human() -> None:
        out_ = ctx.out
        out_.line(f"[green]✓[/green] {entry.id}（{entry.name}）跑完了 · {result.duration_seconds:.1f}s")
        for line in describe(result.record.get("mode") or {}):
            out_.line(f"  [cyan]模式[/cyan] {line}")
        for up in result.record.get("uploads") or []:
            out_.line(f"  [cyan]图片[/cyan] {up['param']} ← {up['local_path']} → {up.get('name')}")
        if result.artifacts:
            for art in result.artifacts:
                size = f"{art.get('width')}×{art.get('height')}" if art.get("width") else ""
                out_.line(f"  {art['type']:<9} {art.get('local_path') or art.get('instance_path')}  {size}")
        else:
            out_.line("  没有产物（工作流里可能没有 Save 节点）")
        out_.line(f"  run_id {result.run_id} → cwf log {result.run_id}")

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("run")
    def run_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="ID / 别名 / 名字 / 唯一前缀"),
        param: list[str] = typer.Option([], "--param", "-p", help="已绑定参数 k=v（可重复）"),
        set_: list[str] = typer.Option([], "--set", help="后门：NODE.INPUT=V（不落库）"),
        preset: list[str] = typer.Option([], "--preset", help="预设，顺序叠加"),
        save_preset: str | None = typer.Option(None, "--save-preset", help="存预设后结束"),
        dry_run: bool = typer.Option(False, "--dry-run", help="只打印 payload 与参数来源"),
        out: str | None = typer.Option(None, "--out", help="产物目录"),
        prefix: str | None = typer.Option(None, "--prefix", help="覆写输出节点 filename_prefix"),
        random_seed: bool = typer.Option(False, "--random-seed", help="随机化 seed 类参数"),
        timeout: int | None = typer.Option(None, "--timeout", help="执行超时（默认取 config）"),
        no_wait: bool = typer.Option(False, "--no-wait", help="提交就返回，不等结果"),
        allow_remote: bool = typer.Option(
            False, "--allow-remote", help="允许往非本机地址提交"
        ),
        enable: list[str] = typer.Option([], "--enable", help="启用被旁路的节点 ID（可重复/逗号分隔）"),
        skip: list[str] = typer.Option([], "--skip", help="这次额外旁路掉的节点 ID"),
    ) -> None:
        """跑一个工作流（第一次会确认，之后不再问）。"""
        run(
            ctx.obj,
            target,
            params=list(param),
            sets=list(set_),
            presets=list(preset),
            save_preset=save_preset,
            dry_run=dry_run,
            out=out,
            prefix=prefix,
            random_seed=random_seed,
            timeout=timeout,
            no_wait=no_wait,
            allow_remote=allow_remote,
            enable=list(enable),
            skip=list(skip),
        )
