"""任务队列：`jobs.jsonl` 事件流 + `state.json` 可重建索引（附录 §6.1）。

只追加、单进程前台、无文件锁；`state.json` 删掉能完全重放出来。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from . import procs
from .errors import CwfError, ExitCode
from .io_utils import atomic_write_json, read_json_or
from .paths import Workspace

STATES = ("pending", "running", "done", "failed", "canceled")


def now() -> datetime:
    return datetime.now()


def iso(ts: datetime | None = None) -> str:
    return (ts or now()).strftime("%Y-%m-%dT%H:%M:%S")


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


# --------------------------------------------------------------------------- 事件


def append_event(ws: Workspace, event: dict[str, Any]) -> None:
    ws.queue_dir.mkdir(parents=True, exist_ok=True)
    event.setdefault("at", iso())
    line = json.dumps(event, ensure_ascii=False)
    with ws.queue_jobs.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def read_events(ws: Workspace) -> list[dict[str, Any]]:
    if not ws.queue_jobs.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in ws.queue_jobs.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue                      # 半截行直接跳过（追加写留下的痕迹）
        if isinstance(raw, dict):
            out.append(raw)
    return out


def rebuild(ws: Workspace) -> dict[str, Any]:
    """从事件流重放。**每一个状态变更都必须有事件**，否则这里就会丢。"""
    state: dict[str, Any] = {
        "rebuilt_at": iso(),
        "paused": False,
        "owner_pid": None,
        "jobs": {},
    }
    for ev in read_events(ws):
        kind = ev.get("ev")
        jid = ev.get("job_id")
        if kind == "pause":
            state["paused"] = bool(ev.get("value"))
            continue
        if kind == "add" and isinstance(ev.get("job"), dict):
            job = dict(ev["job"])
            job.setdefault("state", "pending")
            job.setdefault("attempts", 0)
            job.setdefault("created_at", ev.get("at"))
            state["jobs"][str(job["job_id"])] = job
            continue
        job = state["jobs"].get(str(jid))
        if job is None:
            continue
        if kind == "start":
            job["state"] = "running"
            job["started_at"] = ev.get("at")
            job["attempts"] = int(ev.get("attempt", job.get("attempts", 0)) or 0)
            job["owner_pid"] = ev.get("pid")
        elif kind == "finish":
            job["state"] = "done"
            job["finished_at"] = ev.get("at")
            job["run_id"] = ev.get("run_id")
            job["error"] = None
            job["error_code"] = None
            job["next_attempt_at"] = None
        elif kind == "fail":
            job["error"] = ev.get("error")
            job["error_code"] = ev.get("error_code")
            job["attempts"] = int(ev.get("attempt", job.get("attempts", 0)) or 0)
            if ev.get("retryable") and job["attempts"] < int(job.get("max_attempts", 3)):
                job["state"] = "pending"
                job["next_attempt_at"] = ev.get("next_attempt_at")
            else:
                job["state"] = "failed"
                job["finished_at"] = ev.get("at")
        elif kind == "cancel":
            job["state"] = "canceled"
            job["finished_at"] = ev.get("at")
        elif kind == "retry":
            job["state"] = "pending"
            job["attempts"] = 0
            job["next_attempt_at"] = None
            job["error"] = None
            job["error_code"] = None
            job["finished_at"] = None
        elif kind == "rm":
            state["jobs"].pop(str(jid), None)
        elif kind == "reset":
            job["state"] = "pending"          # 崩溃恢复：running → pending，attempts 不增
            job["started_at"] = None
            job["owner_pid"] = None
    return state


def load_state(ws: Workspace) -> dict[str, Any]:
    cached = read_json_or(ws.queue_state, None)
    if isinstance(cached, dict) and isinstance(cached.get("jobs"), dict):
        return cached
    return rebuild(ws)


def save_state(ws: Workspace, state: dict[str, Any]) -> None:
    state["rebuilt_at"] = iso()
    atomic_write_json(ws.queue_state, state)


# --------------------------------------------------------------------------- 作业


def next_job_id(state: dict[str, Any]) -> str:
    stamp = now().strftime("%Y%m%d-%H%M")
    taken = [j for j in state["jobs"] if str(j).startswith(f"j-{stamp}-")]
    return f"j-{stamp}-{len(taken) + 1:02d}"


def add_job(
    ws: Workspace,
    state: dict[str, Any],
    *,
    workflow_id: str,
    params: dict[str, str],
    preset: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    tag: str | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    job = {
        "job_id": next_job_id(state),
        "workflow_id": workflow_id,
        "params": params,
        "preset": preset,
        "priority": priority,
        "max_attempts": max_attempts,
        "tag": tag,
        "source": source,
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": None,
        "created_at": iso(),
        "started_at": None,
        "finished_at": None,
        "run_id": None,
        "error": None,
        "error_code": None,
    }
    append_event(ws, {"ev": "add", "job": job})
    state["jobs"][job["job_id"]] = job
    save_state(ws, state)
    return job


def due_jobs(state: dict[str, Any], at: datetime | None = None) -> list[dict[str, Any]]:
    moment = at or now()
    out = []
    for job in state["jobs"].values():
        if job.get("state") != "pending":
            continue
        when = parse(job.get("next_attempt_at"))
        if when is not None and when > moment:
            continue
        out.append(job)
    out.sort(key=lambda j: (-int(j.get("priority") or 0), str(j.get("created_at") or ""),
                            str(j.get("job_id"))))
    return out


def backoff_seconds(cfg, attempt: int) -> int:
    seq = list(cfg.queue.backoff_seconds) or [30]
    idx = min(max(attempt - 1, 0), len(seq) - 1)
    return int(seq[idx])


def next_attempt_at(cfg, attempt: int) -> str:
    return iso(now() + timedelta(seconds=backoff_seconds(cfg, attempt)))


def non_retryable(cfg, error_code: str | None, node_errors: dict[str, Any] | None) -> bool:
    wanted = set(cfg.queue.non_retryable_error_types)
    for _node, detail in (node_errors or {}).items():
        if not isinstance(detail, dict):
            continue
        for err in detail.get("errors") or []:
            if isinstance(err, dict) and str(err.get("type")) in wanted:
                return True
        if str(detail.get("type")) in wanted:
            return True
    return error_code in {"INVALID_INPUT", "VALUE_NOT_IN_LIST"}


# --------------------------------------------------------------------------- 运行


def recover(ws: Workspace, state: dict[str, Any]) -> list[str]:
    """上次进程被杀：`running` 且 pid 已死 → 重置为 pending（attempts 不增）。"""
    fixed: list[str] = []
    for job in list(state["jobs"].values()):
        if job.get("state") != "running":
            continue
        if procs.pid_alive(job.get("owner_pid")):
            continue
        append_event(ws, {"ev": "reset", "job_id": job["job_id"]})
        job["state"] = "pending"
        job["started_at"] = None
        job["owner_pid"] = None
        fixed.append(str(job["job_id"]))
    if fixed:
        save_state(ws, state)
    return fixed


def guard_owner(state: dict[str, Any]) -> None:
    owner = state.get("owner_pid")
    if owner and procs.pid_alive(owner):
        raise CwfError(
            f"已经有一个 queue run 在跑（pid {owner}）",
            code=ExitCode.USAGE,
            error_code="QUEUE_BUSY",
            hint="等它结束；或先按 Ctrl+C 停掉那个窗口",
        )


def claim_owner(ws: Workspace, state: dict[str, Any]) -> None:
    state["owner_pid"] = procs.current_pid()
    save_state(ws, state)


def release_owner(ws: Workspace, state: dict[str, Any]) -> None:
    state["owner_pid"] = None
    save_state(ws, state)


def stats(state: dict[str, Any]) -> dict[str, Any]:
    counts = {s: 0 for s in STATES}
    for job in state["jobs"].values():
        counts[job.get("state", "pending")] = counts.get(job.get("state", "pending"), 0) + 1
    return {
        "total": len(state["jobs"]),
        "paused": bool(state.get("paused")),
        "owner_pid": state.get("owner_pid"),
        "states": counts,
        "due": len(due_jobs(state)),
    }
