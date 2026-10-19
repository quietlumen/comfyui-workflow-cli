"""运行记录：写入与查询（`workflows/<ID>/runs/<run_id>.json`）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, read_json_or
from .paths import Workspace


def runs_dir(ws: Workspace, entry_id: str) -> Path:
    return ws.entry_dir(entry_id) / "runs"


def record_path(ws: Workspace, entry_id: str, run_id: str) -> Path:
    return runs_dir(ws, entry_id) / f"{run_id}.json"


def write(ws: Workspace, entry_id: str, record: dict[str, Any]) -> Path:
    path = record_path(ws, entry_id, str(record["run_id"]))
    atomic_write_json(path, record)
    return path


def read(ws: Workspace, entry_id: str, run_id: str) -> dict[str, Any] | None:
    raw = read_json_or(record_path(ws, entry_id, run_id), None)
    return raw if isinstance(raw, dict) else None


def records(ws: Workspace, entry_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    d = runs_dir(ws, entry_id)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), key=lambda p: p.name, reverse=True)
    if limit is not None:
        files = files[:limit]
    out: list[dict[str, Any]] = []
    for path in files:
        raw = read_json_or(path, None)
        if isinstance(raw, dict):
            out.append(raw)
    return out


def find_by_run_id(
    ws: Workspace, run_id: str
) -> tuple[str, dict[str, Any], list[str]] | None:
    """`log <run_id>` 用：在全部条目目录里找，不必先知道属于哪个流。

    返回 `(条目 ID, 记录, 多出来的条目 ID)`。同一个 `run_id` 出现在两个目录只可能是
    手工拷贝导致的；按目录名排序取第一个，同时把其余的报上去（附录 §7.11）。
    """
    if not ws.workflows_dir.is_dir():
        return None
    hits: list[tuple[str, dict[str, Any]]] = []
    for child in sorted(ws.workflows_dir.iterdir()):
        if not child.is_dir():
            continue
        path = child / "runs" / f"{run_id}.json"
        if path.is_file():
            raw = read_json_or(path, None)
            if isinstance(raw, dict):
                hits.append((child.name, raw))
    if not hits:
        return None
    first_id, first = hits[0]
    return first_id, first, [eid for eid, _ in hits[1:]]


def make_run_id(prompt_id: str) -> str:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{prompt_id[:8]}"


def latest_success_ids(ws: Workspace, entry_id: str) -> set[str]:
    """批量续跑用：这个流所有成功记录的 `batch._id`。"""
    out: set[str] = set()
    for rec in records(ws, entry_id):
        if rec.get("status") != "success":
            continue
        batch = rec.get("batch") or {}
        if batch.get("_id"):
            out.add(str(batch["_id"]))
    return out


def ensure_dir(ws: Workspace, entry_id: str) -> Path:
    d = runs_dir(ws, entry_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def summary(record: dict[str, Any]) -> str:
    status = record.get("status")
    dur = record.get("duration_seconds")
    arts = len(record.get("artifacts") or [])
    bits = [str(status)]
    if isinstance(dur, (int, float)):
        bits.append(f"{dur:.1f}s")
    bits.append(f"{arts} 产物")
    return " · ".join(bits)


def as_json(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, indent=2)
