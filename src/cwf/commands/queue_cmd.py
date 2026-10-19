"""`cwf queue`（PRD §7 FR-5）：入队 / 前台消费 / 重试 / 断点续跑。"""

from __future__ import annotations

import time
from typing import Any

import typer
from rich.table import Table

from .. import queue as q
from ..config import load
from ..context import Ctx
from ..client import guard_remote
from ..errors import CwfError, ExitCode, not_initialized, usage
from ..executor import prepare, run_once
from ..ledger import iter_entries
from ..output import payload
from ..parameters import available_names, merge_values, parse_kv, resolve_param
from ..prompts import gate, require_yes
from ..resolve import resolve

queue_app = typer.Typer(add_completion=False, help="任务队列（前台进程，不常驻）")


def _load(ctx: Ctx) -> tuple[Any, dict[str, Any]]:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    return load(ws), q.load_state(ws)


# --------------------------------------------------------------------------- add


def add(
    ctx: Ctx,
    target: str,
    *,
    params: list[str],
    preset: str | None,
    priority: int,
    max_attempts: int,
    tag: str | None,
) -> None:
    cfg, state = _load(ctx)
    entry = resolve(iter_entries(ctx.ws), target)
    cli_params = parse_kv(params)
    # 入队前先校验参数名，别让错行在半夜才被发现
    merge_values(entry.params, {}, [preset] if preset else [], cli_params)
    for token in cli_params:
        if resolve_param(entry.params, token) is None:
            raise usage(f"--param {token} 不是已绑定的参数", f"可用：{available_names(entry.params)}")

    job = q.add_job(
        ctx.ws,
        state,
        workflow_id=entry.id,
        params=cli_params,
        preset=preset,
        priority=priority,
        max_attempts=max_attempts or cfg.queue.max_attempts,
        tag=tag,
    )
    data = payload(job=job, stats=q.stats(state))

    def human() -> None:
        ctx.out.line(f"[green]✓[/green] 已入队 {job['job_id']}（{entry.id} {entry.name}）")
        if cli_params:
            ctx.out.line("  参数 " + ", ".join(f"{k}={v}" for k, v in cli_params.items()))
        ctx.out.line(f"  队列：{q.stats(state)['due']} 个待跑。cwf queue run 开始消费。")

    ctx.out.emit(data, human)


# --------------------------------------------------------------------------- run


def run(
    ctx: Ctx,
    *,
    until_empty: bool,
    max_jobs: int | None,
    poll: float | None,
    allow_remote: bool = False,
) -> None:
    cfg, state = _load(ctx)
    guard_remote(cfg, allow=allow_remote, command="queue run")
    q.guard_owner(state)
    fixed = q.recover(ctx.ws, state)
    if fixed:
        ctx.out.line(f"[yellow]![/yellow] 上次中断的作业已重新排队：{', '.join(fixed)}")

    if state.get("paused"):
        raise usage("队列是暂停状态", "cwf queue resume 之后再跑")

    if not until_empty:
        require_yes(
            ctx,
            "开始消费队列？",
            details=["前台进程，Ctrl+C 停；当前作业跑完才停"],
        )

    q.claim_owner(ctx.ws, state)
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    ran = 0
    interval = poll if poll is not None else cfg.queue.poll_interval
    try:
        while True:
            if max_jobs is not None and ran >= max_jobs:
                break
            ready = q.due_jobs(state)
            if not ready:
                if until_empty:
                    waiting = [
                        q.parse(j.get("next_attempt_at"))
                        for j in state["jobs"].values()
                        if j.get("state") == "pending" and q.parse(j.get("next_attempt_at"))
                    ]
                    if not waiting:
                        break
                    nxt = min(waiting)
                    wait = max(0.0, (nxt - q.now()).total_seconds())
                    ctx.out.line(f"[dim]等 {wait:.0f}s 到下一个作业的重试时间…[/dim]")
                    time.sleep(min(wait, interval))
                    continue
                break
            job = ready[0]
            ran += 1
            _run_one(ctx, cfg, state, job, done=done, failed=failed)
    finally:
        q.release_owner(ctx.ws, state)

    snapshot = q.stats(state)
    if failed:
        # 部分失败：只输出一个 JSON（错误对象里带清单），不再另打一份成功载荷
        raise CwfError(
            f"{len(failed)} 个作业失败（本次跑了 {ran} 个，成功 {len(done)}）",
            code=ExitCode.ERROR,
            error_code="QUEUE_PARTIAL",
            hint="cwf queue list --state failed；cwf queue retry --failed 可重排队",
            extra={
                "ran": ran,
                "done": len(done),
                "failed": len(failed),
                "done_items": done,
                "failed_items": failed,
                "stats": snapshot,
            },
        )

    data = payload(ran=ran, done=done, failed=[], stats=snapshot)

    def human() -> None:
        if not ran:
            ctx.out.line("没有到期的作业。")
            return
        ctx.out.line(f"跑了 {ran} 个：成功 {len(done)} · 失败 0")
        for item in done:
            ctx.out.line(f"  [green]✓[/green] {item['job_id']} → {item.get('run_id')}")

    ctx.out.emit(data, human)


def _run_one(ctx: Ctx, cfg, state: dict[str, Any], job: dict[str, Any], *, done, failed) -> None:
    from .. import queue as qq

    job_id = job["job_id"]
    entry = None
    for candidate in iter_entries(ctx.ws):
        if candidate.id == job["workflow_id"]:
            entry = candidate
            break
    if entry is None:
        qq.append_event(ctx.ws, {"ev": "fail", "job_id": job_id, "error": "工作流已不在台账",
                                 "error_code": "NO_WORKFLOW", "retryable": False})
        job.update({"state": "failed", "error": "工作流已不在台账"})
        qq.save_state(ctx.ws, state)
        failed.append({"job_id": job_id, "error": "工作流已不在台账"})
        return

    attempt = int(job.get("attempts") or 0) + 1
    # PRD §10 的两道关口也管队列：首次未验证 + 高成本作业在**真跑之前**问一次
    try:
        preview = prepare(
            entry,
            cli_params=dict(job.get("params") or {}),
            presets=[job["preset"]] if job.get("preset") else [],
            cfg=None,
        )
    except CwfError:
        preview = None
    try:
        gate(ctx, entry, [preview.payload] if preview is not None else [])
    except CwfError as exc:
        # 用户在关口上拒绝：作业留在 pending，不算一次执行（attempts 不动）
        qq.append_event(ctx.ws, {"ev": "fail", "job_id": job_id, "error": exc.message,
                                 "error_code": exc.error_code, "retryable": True})
        job.update({"state": "pending", "error": exc.message,
                    "error_code": exc.error_code})
        qq.save_state(ctx.ws, state)
        if exc.error_code != "CANCELED":
            failed.append({"job_id": job_id, "error": exc.message})
        return

    qq.append_event(ctx.ws, {"ev": "start", "job_id": job_id, "attempt": attempt,
                             "pid": __import__("os").getpid()})
    job.update({"state": "running", "attempts": attempt, "started_at": qq.iso()})
    qq.save_state(ctx.ws, state)
    try:
        result = run_once(
            ctx, cfg, entry,
            cli_params=dict(job.get("params") or {}),
            presets=[job["preset"]] if job.get("preset") else [],
            client_id=f"cwf-queue-{job_id}",
            job_meta={"job_id": job_id, "attempt": attempt},
            tag=job.get("tag"),
        )
        run_id = getattr(result, "run_id", None)
        qq.append_event(ctx.ws, {"ev": "finish", "job_id": job_id, "run_id": run_id})
        job.update({"state": "done", "run_id": run_id, "finished_at": qq.iso()})
        done.append({"job_id": job_id, "run_id": run_id})
    except CwfError as exc:
        retryable = not qq.non_retryable(cfg, exc.error_code, exc.node_errors)
        will_retry = retryable and attempt < int(job.get("max_attempts") or 3)
        next_at = qq.next_attempt_at(cfg, attempt) if will_retry else None
        qq.append_event(
            ctx.ws,
            {"ev": "fail", "job_id": job_id, "attempt": attempt, "error": exc.message,
             "error_code": exc.error_code, "retryable": retryable,
             "next_attempt_at": next_at},
        )
        job.update(
            {
                "state": "pending" if will_retry else "failed",
                "attempts": attempt,
                "error": exc.message,
                "error_code": exc.error_code,
                "next_attempt_at": next_at,
            }
        )
        if not will_retry:
            failed.append({"job_id": job_id, "error": exc.message})
    qq.save_state(ctx.ws, state)


# --------------------------------------------------------------------------- 查询


def list_jobs(ctx: Ctx, *, states: list[str], tag: str | None, limit: int | None) -> None:
    _cfg, state = _load(ctx)
    jobs = list(state["jobs"].values())
    if states:
        jobs = [j for j in jobs if j.get("state") in states]
    if tag:
        jobs = [j for j in jobs if j.get("tag") == tag]
    jobs.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    total = len(jobs)
    if limit:
        jobs = jobs[:limit]
    data = payload(count=total, items=jobs, stats=q.stats(state))

    def human() -> None:
        if not jobs:
            ctx.out.line("队列里没有匹配的作业。")
            return
        table = Table(box=None, pad_edge=False, header_style="bold")
        for col in ("job_id", "工作流", "状态", "尝试", "下次重试", "参数"):
            table.add_column(col)
        for j in jobs:
            params = ", ".join(f"{k}={v}" for k, v in (j.get("params") or {}).items())
            table.add_row(
                str(j["job_id"]), str(j.get("workflow_id")), str(j.get("state")),
                f"{j.get('attempts')}/{j.get('max_attempts')}",
                str(j.get("next_attempt_at") or "-"), params[:40],
            )
        ctx.out.console.print(table)
        ctx.out.line(f"{total} 个作业。cwf queue run 开始消费。")

    ctx.out.emit(data, human)


def show_job(ctx: Ctx, job_id: str) -> None:
    _cfg, state = _load(ctx)
    job = state["jobs"].get(job_id)
    if job is None:
        raise CwfError(f"没有这个作业：{job_id}", code=ExitCode.USAGE, error_code="NOT_FOUND")
    ctx.out.emit(payload(job=job), lambda: ctx.out.line(
        __import__("json").dumps(job, ensure_ascii=False, indent=2)
    ))


def stats_cmd(ctx: Ctx) -> None:
    _cfg, state = _load(ctx)
    data = payload(**q.stats(state))
    ctx.out.emit(data, lambda: ctx.out.line(__import__("json").dumps(data, ensure_ascii=False, indent=2)))


# --------------------------------------------------------------------------- 改状态


def retry_jobs(ctx: Ctx, *, job_id: str | None, failed_only: bool) -> None:
    _cfg, state = _load(ctx)
    if failed_only:
        targets = [j for j in state["jobs"].values() if j.get("state") == "failed"]
    elif job_id:
        job = state["jobs"].get(job_id)
        if job is None:
            raise CwfError(f"没有这个作业：{job_id}", code=ExitCode.USAGE, error_code="NOT_FOUND")
        targets = [job]
    else:
        raise usage("要给 <JOB> 或 --failed")
    for job in targets:
        q.append_event(ctx.ws, {"ev": "retry", "job_id": job["job_id"]})
        job.update({"state": "pending", "attempts": 0, "next_attempt_at": None,
                    "error": None, "error_code": None, "finished_at": None})
    q.save_state(ctx.ws, state)
    ids = [str(j["job_id"]) for j in targets]
    ctx.out.emit(payload(retried=ids), lambda: ctx.out.line(
        f"[green]✓[/green] 重新排队 {len(ids)} 个：{', '.join(ids)}"
    ))


def cancel_jobs(ctx: Ctx, *, job_id: str | None, all_: bool) -> None:
    _cfg, state = _load(ctx)
    if all_:
        targets = [j for j in state["jobs"].values() if j.get("state") in ("pending", "running")]
        require_yes(ctx, f"取消 {len(targets)} 个未完成作业？")
    elif job_id:
        job = state["jobs"].get(job_id)
        if job is None:
            raise CwfError(f"没有这个作业：{job_id}", code=ExitCode.USAGE, error_code="NOT_FOUND")
        targets = [job]
    else:
        raise usage("要给 <JOB> 或 --all")
    for job in targets:
        q.append_event(ctx.ws, {"ev": "cancel", "job_id": job["job_id"]})
        job.update({"state": "canceled", "finished_at": q.iso()})
    q.save_state(ctx.ws, state)
    ctx.out.emit(payload(canceled=[str(j["job_id"]) for j in targets]),
                 lambda: ctx.out.line(f"[green]✓[/green] 取消 {len(targets)} 个作业"))


def rm_jobs(ctx: Ctx, *, job_id: str | None, state_filter: str | None, all_: bool) -> None:
    _cfg, state = _load(ctx)
    if all_:
        targets = list(state["jobs"].values())
    elif state_filter:
        targets = [j for j in state["jobs"].values() if j.get("state") == state_filter]
    elif job_id:
        job = state["jobs"].get(job_id)
        if job is None:
            raise CwfError(f"没有这个作业：{job_id}", code=ExitCode.USAGE, error_code="NOT_FOUND")
        targets = [job]
    else:
        raise usage("要给 <JOB>、--state 或 --all")
    require_yes(ctx, f"从索引移除 {len(targets)} 个作业？（jsonl 事件保留）")
    for job in targets:
        q.append_event(ctx.ws, {"ev": "rm", "job_id": job["job_id"]})
        state["jobs"].pop(job["job_id"], None)
    q.save_state(ctx.ws, state)
    ctx.out.emit(payload(removed=len(targets)),
                 lambda: ctx.out.line(f"[green]✓[/green] 移除 {len(targets)} 个作业（事件流保留）"))


def pause(ctx: Ctx, value: bool) -> None:
    _cfg, state = _load(ctx)
    q.append_event(ctx.ws, {"ev": "pause", "value": value})
    state["paused"] = value
    q.save_state(ctx.ws, state)
    ctx.out.emit(payload(paused=value), lambda: ctx.out.line(
        f"[green]✓[/green] 队列{'暂停' if value else '恢复'}"
    ))


# --------------------------------------------------------------------------- 注册


@queue_app.command("add")
def _add(
    ctx: typer.Context,
    target: str = typer.Argument(...),
    param: list[str] = typer.Option([], "--param", "-p"),
    preset: str | None = typer.Option(None, "--preset"),
    priority: int = typer.Option(0, "--priority"),
    max_attempts: int = typer.Option(0, "--max-attempts"),
    tag: str | None = typer.Option(None, "--tag"),
) -> None:
    """把一个调用排进队列。"""
    add(ctx.obj, target, params=list(param), preset=preset, priority=priority,
        max_attempts=max_attempts, tag=tag)


@queue_app.command("run")
def _run(
    ctx: typer.Context,
    until_empty: bool = typer.Option(False, "--until-empty", help="连退避等待的也等完"),
    max_jobs: int | None = typer.Option(None, "--max", help="本次最多跑几个"),
    poll: float | None = typer.Option(None, "--poll", help="轮询间隔（秒）"),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="允许往非本机地址提交"
    ),
) -> None:
    """前台消费队列（Ctrl+C 停，当前作业跑完即停）。"""
    run(ctx.obj, until_empty=until_empty, max_jobs=max_jobs, poll=poll,
        allow_remote=allow_remote)


@queue_app.command("list")
def _list(
    ctx: typer.Context,
    state: list[str] = typer.Option([], "--state"),
    tag: str | None = typer.Option(None, "--tag"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    """看队列里有哪些作业。"""
    list_jobs(ctx.obj, states=list(state), tag=tag, limit=limit)


@queue_app.command("show")
def _show(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """看单个作业的完整字段。"""
    show_job(ctx.obj, job_id)


@queue_app.command("stats")
def _stats(ctx: typer.Context) -> None:
    """各状态计数。"""
    stats_cmd(ctx.obj)


@queue_app.command("retry")
def _retry(
    ctx: typer.Context,
    job_id: str | None = typer.Argument(None),
    failed: bool = typer.Option(False, "--failed"),
) -> None:
    """重新排队（attempts 归零）。"""
    retry_jobs(ctx.obj, job_id=job_id, failed_only=failed)


@queue_app.command("cancel")
def _cancel(
    ctx: typer.Context,
    job_id: str | None = typer.Argument(None),
    all_: bool = typer.Option(False, "--all"),
) -> None:
    """取消未完成的作业。"""
    cancel_jobs(ctx.obj, job_id=job_id, all_=all_)


@queue_app.command("rm")
def _rm(
    ctx: typer.Context,
    job_id: str | None = typer.Argument(None),
    state: str | None = typer.Option(None, "--state"),
    all_: bool = typer.Option(False, "--all"),
) -> None:
    """从索引移除（jsonl 事件保留）。"""
    rm_jobs(ctx.obj, job_id=job_id, state_filter=state, all_=all_)


@queue_app.command("pause")
def _pause(ctx: typer.Context) -> None:
    """暂停消费。"""
    pause(ctx.obj, True)


@queue_app.command("resume")
def _resume(ctx: typer.Context) -> None:
    """恢复消费。"""
    pause(ctx.obj, False)


def register(app: typer.Typer) -> None:
    app.add_typer(queue_app, name="queue")
