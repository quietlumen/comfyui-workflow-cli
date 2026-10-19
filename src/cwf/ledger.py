"""台账读取与状态推导（只读；附录 §3）。

M0 阶段台账一定是空的，但读取层先立起来，后面 `list`/`show`/`status` 都复用它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .io_utils import read_json_or
from .paths import Workspace

STATES = ("ready", "stale", "captured", "registered", "missing")


@dataclass
class Entry:
    id: str
    dir: Path
    meta: dict[str, Any]
    params: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str(self.meta.get("name") or self.id)

    @property
    def origin(self) -> str:
        return str(self.meta.get("origin") or "instance")

    @property
    def function(self) -> str:
        return str(self.meta.get("function") or "other")

    @property
    def api_file(self) -> Path:
        return self.dir / "api.json"

    @property
    def source_file(self) -> Path:
        return self.dir / "source.json"

    @property
    def state(self) -> str:
        return entry_state(self.meta, self.params, self.api_file)


def entry_state(meta: dict[str, Any], params: list[dict[str, Any]], api_file: Path) -> str:
    """五态 + `origin: api` 两态（附录 §3）。"""
    if (meta.get("origin") or "instance") == "api":
        return "ready" if params else "captured"

    source = meta.get("source") or {}
    if not source.get("present", True):
        return "missing"
    if not api_file.is_file():
        return "registered"
    if not params:
        return "captured"
    if meta.get("api_stale"):
        return "stale"
    return "ready"


def iter_entries(ws: Workspace) -> list[Entry]:
    """扫 `workflows/*/meta.json`；没有 meta.json 的目录（只剩 runs/）不算条目。"""
    root = ws.workflows_dir
    if not root.is_dir():
        return []
    entries: list[Entry] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        meta = read_json_or(child / "meta.json", None)
        if not isinstance(meta, dict):
            continue
        entry_id = str(meta.get("id") or child.name)
        params = read_json_or(child / "params.json", [])
        if not isinstance(params, list):
            params = []
        entries.append(Entry(id=entry_id, dir=child, meta=meta, params=params))
    return entries


def state_counts(entries: list[Entry]) -> dict[str, int]:
    counts = {s: 0 for s in STATES}
    for e in entries:
        counts[e.state] = counts.get(e.state, 0) + 1
    return counts


def origin_counts(entries: list[Entry]) -> dict[str, int]:
    counts = {"instance": 0, "api": 0}
    for e in entries:
        counts[e.origin] = counts.get(e.origin, 0) + 1
    return counts


def recent_runs(entry: Entry, limit: int = 5) -> list[dict[str, Any]]:
    """该条目自己目录下的运行记录，最新的在前。"""
    runs_dir = entry.dir / "runs"
    if not runs_dir.is_dir():
        return []
    files = sorted(runs_dir.glob("*.json"), key=lambda p: p.name, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for path in files:
        raw = read_json_or(path, None)
        if not isinstance(raw, dict):
            continue
        out.append(
            {
                "run_id": raw.get("run_id") or path.stem,
                "status": raw.get("status"),
                "at": raw.get("finished_at") or raw.get("started_at"),
                "duration_seconds": raw.get("duration_seconds"),
                "params": raw.get("params") or {},
                "artifacts": len(raw.get("artifacts") or []),
            }
        )
    return out


def state_reasons(meta: dict[str, Any], params: list[dict[str, Any]], api_file: Path) -> list[str]:
    """把状态推导过程讲成人话，给 `show` 用。"""
    origin = meta.get("origin") or "instance"
    if origin == "api":
        return [
            "来源：导入的 API 文件（没有源文件）",
            "有 api.json",
            *(["已绑 %d 个参数" % len(params)] if params else ["还没绑参数"]),
        ]
    source = meta.get("source") or {}
    reasons = [
        "源文件在：%s" % ("是" if source.get("present", True) else "**否（missing）**"),
        "有 api.json：%s" % ("是" if api_file.is_file() else "否"),
        "已绑参数：%d" % len(params),
    ]
    if meta.get("api_stale"):
        reasons.append("api.json 比源文件旧（stale）")
    return reasons
