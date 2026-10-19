"""`cwf batch`（PRD §7 FR-4）：一行一次调用，失败隔离，可断点续跑。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import typer
from rich.table import Table

from .. import batch, logstore
from ..client import guard_remote
from ..config import load
from ..context import Ctx
from ..errors import CwfError, ExitCode, not_initialized, usage
from ..executor import complete_no_wait, load_presets, prepare, run_once
from ..parameters import available_names, merge_values
from ..io_utils import atomic_write_json
from ..ledger import iter_entries
from ..output import payload
from ..prompts import gate, require_yes
from ..resolve import resolve


def run(
    ctx: Ctx,
    target: str,
    *,
    csv_path: str | None,
    jsonl_path: str | None,
    matrix: str | None,
    resume: bool,
    on_error: str | None,
    out: str | None,
    tag: str | None,
    dry_run: bool,
    queue: bool,
    prefix: str | None,
    timeout: int | None,
    concurrency: int,
    preset: str | None,
    allow_remote: bool,
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    cfg = load(ws)
    guard_remote(cfg, allow=allow_remote, command="batch")
    if resume and queue:
        raise usage("--resume 与 --queue 互斥", "--queue 只负责投递，续跑判断留给队列自己")
    if concurrency < 1:
        raise usage("--concurrency 至少是 1", "它只控提交节奏，不是并行采样")

    driver, rows = batch.parse_drivers(
        csv_path=csv_path, jsonl_path=jsonl_path, matrix=matrix
    )
    entry = resolve(iter_entries(ws), target)
    strategy = on_error or cfg.batch.on_error
    if strategy not in ("continue", "stop"):
        raise usage("--on-error 只能是 continue | stop")

    presets_map = load_presets(entry)
    done = logstore.latest_success_ids(ws, entry.id) if resume else set()
    skipped: list[dict[str, Any]] = [
        {"_id": r._id, "index": r.index, "reason": "已经有成功记录（--resume）"}
        for r in rows
        if r._id in done
    ]
    # 先整体校验参数名，非法行不进提交循环（避免跑到第 80 行才发现第 3 行写错了）
    todo: list[batch.Row] = []
    invalid: list[dict[str, Any]] = []
    for r in rows:
        if r._id in done:
            continue
        try:
            # 行内 _preset 与 --preset 都要参与校验：预设里写错的参数名同样是错行
            merge_values(
                entry.params, presets_map,
                [x for x in (r.preset, preset) if x], dict(r.params),
            )
        except CwfError as exc:
            invalid.append({"_id": r._id, "index": r.index, "reason": exc.message})
            continue
        todo.append(r)
    skipped.extend(invalid)
    if not todo and invalid:
        raise usage(
            f"所有行都不合法：{invalid[0]['reason']}",
            f"可用参数：{available_names(entry.params)}",
        )

    if dry_run:
        _emit(
            ctx,
            entry,
            driver,
            rows,
            [],
            [],
            skipped,
            dry_run=True,
            batch_id=None,
        )
        return

    if queue:
        if len(todo) >= cfg.batch.confirm_over:
            require_yes(
                ctx,
                f"要把 {len(todo)} 行投进队列？",
                details=[f"工作流 {entry.id}（{entry.name}）", "投递后不自动消费，用 cwf queue run 开始跑"],
            )
        jobs = _enqueue(ctx, cfg, entry, todo, preset=preset, tag=tag)
        _emit_queue(ctx, entry, driver, rows, jobs, skipped)
        return

    if len(todo) >= cfg.batch.confirm_over:
        require_yes(
            ctx,
            f"要提交 {len(todo)} 行，继续？",
            details=[f"工作流 {entry.id}（{entry.name}）",
                     f"提交节奏 {concurrency}（采样仍然串行）"],
        )
    # 高成本关口看的是"每行真的会发出去的包"（`cfg=None` 只组包、不上传）
    gate(ctx, entry, _preview_payloads(entry, todo, preset=preset, prefix=prefix))

    batch_id = batch.new_batch_id()
    started_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ok, failed = _execute(
        ctx, cfg, entry, todo, batch_id=batch_id, preset=preset, tag=tag,
        strategy=strategy, prefix=prefix, out=out, timeout=timeout,
        concurrency=concurrency,
    )

    summary = {
        "batch_id": batch_id,
        "workflow_id": entry.id,
        "workflow_name": entry.name,
        "driver": driver,
        "source": csv_path or jsonl_path or matrix,
        "tag": tag,
        "preset": preset,
        "concurrency": concurrency,
        "total": len(rows),
        "submitted": len(ok) + len(failed),
        "succeeded": len(ok),
        "failed": len(failed),
        "skipped": len(skipped),
        "resume": resume,
        "on_error": strategy,
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ok": ok,
        "failures": failed,
        "skipped_items": skipped,
    }
    atomic_write_json(batch.summary_path(ws, batch_id), summary)

    if failed:
        # 部分失败：**只输出一个** JSON（错误对象里带完整清单），不要再打一份成功载荷
        raise CwfError(
            f"{len(failed)} 行失败（共 {len(rows)} 行，成功 {len(ok)}）",
            code=ExitCode.ERROR,
            error_code="BATCH_PARTIAL",
            hint=f"cwf history {entry.id} 看每次运行；汇总在 batch/{batch_id}.json",
            extra={
                "batch_id": batch_id,
                "workflow_id": entry.id,
                "driver": driver,
                "total": len(rows),
                "succeeded": len(ok),
                "failed": len(failed),
                "skipped": len(skipped),
                "failures": failed,
                "summary_path": str(batch.summary_path(ws, batch_id)),
            },
        )
    _emit(ctx, entry, driver, rows, ok, [], skipped, dry_run=False, batch_id=batch_id)


def _execute(
    ctx: Ctx,
    cfg,
    entry,
    todo: list[batch.Row],
    *,
    batch_id: str,
    preset: str | None,
    tag: str | None,
    strategy: str,
    prefix: str | None,
    out: str | None,
    timeout: int | None,
    concurrency: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 `--concurrency` 的节奏提交。

    N>1 **不是并行采样**（附录 §10.7）：`/prompt` 立刻返回 `prompt_id`，所以这里只是
    "连发 N 个 POST 再统一轮询"。实现上直接借 `run_once(wait=False)` 拿到 prompt_id
    与已落盘的 `running` 记录，再用 `complete_no_wait` 逐条补齐——**没有任何线程**。

    N=1 走原来的直通路径（`wait=True`），行为与 M4 完全一致。
    """
    ok: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if concurrency <= 1:
        for row in todo:
            try:
                ok.append(
                    _run_row(
                        ctx, cfg, entry, row, batch_id=batch_id, preset=preset,
                        tag=tag, prefix=prefix, out=out, timeout=timeout,
                    )
                )
            except CwfError as exc:
                failed.append(_fail_item(row, exc))
                if strategy == "stop":
                    break
        return ok, failed

    interrupted = False
    for start in range(0, len(todo), concurrency):
        if interrupted:
            break
        wave = todo[start:start + concurrency]
        pending: list[tuple[batch.Row, Any]] = []
        for row in wave:
            try:
                pending.append(
                    (
                        row,
                        _run_row(
                            ctx, cfg, entry, row, batch_id=batch_id, preset=preset,
                            tag=tag, prefix=prefix, out=out, timeout=timeout,
                            wait=False,
                        ),
                    )
                )
            except CwfError as exc:
                failed.append(_fail_item(row, exc))
                if strategy == "stop":
                    # 已经发出去的那几个还得等完：本地报错不该留下孤儿 prompt。
                    interrupted = True
                    break
        for row, result in pending:
            try:
                final = complete_no_wait(
                    ctx, cfg, entry, result.run_id, timeout=timeout, out_dir=out
                )
                ok.append(
                    {
                        "_id": row._id,
                        "index": row.index,
                        "run_id": final.run_id,
                        "artifacts": [
                            a.get("local_path") for a in (final.artifacts or [])
                        ],
                    }
                )
            except CwfError as exc:
                failed.append(_fail_item(row, exc))
                if strategy == "stop":
                    # 已提交的照常等完（不管成功失败），只是不再提交下一波。
                    interrupted = True
    return ok, failed


def _run_row(
    ctx: Ctx, cfg, entry, row: batch.Row, *, batch_id: str, preset: str | None,
    tag: str | None, prefix: str | None, out: str | None, timeout: int | None,
    wait: bool = True,
):
    result = run_once(
        ctx,
        cfg,
        entry,
        cli_params=dict(row.params),
        presets=[x for x in (row.preset, preset) if x],
        overrides=[],
        prefix=prefix,
        out_dir=out,
        timeout=timeout,
        wait=wait,
        client_id=f"cwf-batch-{batch_id}",
        batch_meta={"batch_id": batch_id, "_id": row._id, "row": row.index},
        tag=row.tag or tag,
    )
    if wait:
        return {
            "_id": row._id,
            "index": row.index,
            "run_id": getattr(result, "run_id", None),
            "artifacts": [a.get("local_path") for a in getattr(result, "artifacts", [])],
        }
    return result


def _preview_payloads(
    entry, todo: list[batch.Row], *, preset: str | None, prefix: str | None
) -> list[dict[str, Any]]:
    """只为确认关口组包（不连实例、不上传）。

    组不出来（比如某行参数本身有问题）就跳过——真正的报错交给提交流程，
    确认关口不该替它当第一个报错的地方。
    """
    payloads: list[dict[str, Any]] = []
    for row in todo:
        try:
            prepared = prepare(
                entry,
                cli_params=dict(row.params),
                presets=[x for x in (row.preset, preset) if x],
                prefix=prefix,
                cfg=None,
            )
        except CwfError:
            continue
        payloads.append(prepared.payload)
    return payloads


def _fail_item(row: batch.Row, exc: CwfError) -> dict[str, Any]:
    return {
        "_id": row._id,
        "index": row.index,
        "error": exc.message,
        "error_code": exc.error_code,
    }


def _enqueue(ctx: Ctx, cfg, entry, todo: list[batch.Row], *, preset: str | None,
             tag: str | None) -> list[dict[str, Any]]:
    """把展开后的每一行投进队列（等价 `queue add` 若干次）。"""
    from .. import queue as qq

    state = qq.load_state(ctx.ws)
    qq.guard_owner(state)
    jobs: list[dict[str, Any]] = []
    for row in todo:
        job = qq.add_job(
            ctx.ws,
            state,
            workflow_id=entry.id,
            params=dict(row.params),
            preset=row.preset or preset,
            priority=0,
            max_attempts=cfg.queue.max_attempts,
            tag=row.tag or tag,
            source="batch",
        )
        jobs.append(
            {"_id": row._id, "index": row.index, "job_id": job["job_id"]}
        )
    return jobs


def _emit_queue(ctx, entry, driver, rows, jobs, skipped) -> None:
    def human() -> None:
        out = ctx.out
        out.heading(f"{entry.id}（{entry.name}）· {driver} · 投队列")
        out.line(f"投递 {len(jobs)} 个作业 · 跳过 {len(skipped)} 行（共 {len(rows)} 行）")
        for j in jobs[:10]:
            out.line(f"  {j['_id']} → {j['job_id']}")
        if len(jobs) > 10:
            out.line(f"  … 另有 {len(jobs) - 10} 个")
        if jobs:
            out.line("")
            out.line("下一步：cwf queue run 开始消费（前台进程，Ctrl+C 停）")

    ctx.out.emit(
        payload(
            workflow_id=entry.id,
            driver=driver,
            queued=len(jobs),
            skipped=len(skipped),
            jobs=jobs,
            skipped_items=skipped,
            total=len(rows),
        ),
        human,
    )


def _emit(ctx, entry, driver, rows, ok, failed, skipped, *, dry_run, batch_id) -> None:
    data = payload(
        batch_id=batch_id,
        workflow_id=entry.id,
        driver=driver,
        dry_run=dry_run,
        total=len(rows),
        submitted=len(ok) + len(failed),
        succeeded=len(ok),
        failed=len(failed),
        skipped=len(skipped),
        items=[{"_id": r._id, "index": r.index, "params": r.params} for r in rows],
        results=ok,
        failures=failed,
        skipped_items=skipped,
    )

    def human() -> None:
        out = ctx.out
        head = f"{entry.id}（{entry.name}）· {driver}"
        out.heading(head + ("（dry-run，不会提交）" if dry_run else ""))
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("_id")
        table.add_column("参数")
        for r in rows[:50]:
            short = ", ".join(f"{k}={v}" for k, v in r.params.items())
            table.add_row(r._id, short[:80])
        out.console.print(table)
        if dry_run:
            out.line(f"共 {len(rows)} 行，将按顺序各提交一次。")
            return
        out.line(
            f"成功 {len(ok)} · 失败 {len(failed)} · 跳过 {len(skipped)}"
            f"（共 {len(rows)} 行）"
        )
        for f in failed[:5]:
            out.line(f"  [red]✗[/red] {f['_id']}：{f['error']}")
        if ok and not failed:
            out.line("  下一次想跳过已跑过的行：加 --resume")
        if skipped:
            out.line("  跳过明细：--json 里的 skipped_items")
        if batch_id:
            out.line(f"  汇总：batch/{batch_id}.json")

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("batch")
    def batch_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="工作流 ID"),
        csv_path: str | None = typer.Option(None, "--csv", help="CSV 驱动（首行表头）"),
        jsonl_path: str | None = typer.Option(None, "--jsonl", help="JSONL 驱动（每行一个对象）"),
        matrix: str | None = typer.Option(None, "--matrix", help='矩阵："k=a,b; k2=x,y"'),
        resume: bool = typer.Option(False, "--resume", help="跳过已有成功记录的行"),
        on_error: str | None = typer.Option(None, "--on-error", help="continue | stop"),
        out: str | None = typer.Option(None, "--out", help="产物目录"),
        tag: str | None = typer.Option(None, "--tag", help="批次标记"),
        dry_run: bool = typer.Option(False, "--dry-run", help="只列出将提交的行"),
        queue: bool = typer.Option(False, "--queue", help="不立即执行，展开后投进队列"),
        prefix: str | None = typer.Option(None, "--prefix", help="覆写 filename_prefix"),
        timeout: int | None = typer.Option(None, "--timeout", help="单行执行超时"),
        concurrency: int = typer.Option(
            1, "--concurrency", help="提交节奏 N（默认 1；不并行采样）"
        ),
        preset: str | None = typer.Option(None, "--preset", help="整批套用的预设名"),
        allow_remote: bool = typer.Option(
            False, "--allow-remote", help="允许往非本机地址提交"
        ),
    ) -> None:
        """批量跑一个工作流：一行一次调用，失败隔离，可续跑。"""
        run(
            ctx.obj,
            target,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            matrix=matrix,
            resume=resume,
            on_error=on_error,
            out=out,
            tag=tag,
            dry_run=dry_run,
            queue=queue,
            prefix=prefix,
            timeout=timeout,
            concurrency=concurrency,
            preset=preset,
            allow_remote=allow_remote,
        )
