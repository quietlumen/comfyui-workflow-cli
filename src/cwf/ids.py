"""工作流 ID 分配（`workflows/_ids.json`，永不复用）。"""

from __future__ import annotations

import re
from pathlib import Path

from .io_utils import atomic_write_json, read_json_or
from .paths import Workspace

_ID_RE = re.compile(r"^wf-(\d{4,})$")


def _existing_numbers(ws: Workspace) -> list[int]:
    root = ws.workflows_dir
    if not root.is_dir():
        return []
    numbers: list[int] = []
    for child in root.iterdir():
        m = _ID_RE.match(child.name)
        if child.is_dir() and m:
            numbers.append(int(m.group(1)))
    return numbers


def read_next(ws: Workspace) -> int:
    """水位；文件缺失/损坏/落后于现存目录 → 用 `max(现存)+1` 兜底。"""
    floor = max(_existing_numbers(ws), default=0) + 1
    raw = read_json_or(ws.ids_file, None)
    if isinstance(raw, dict):
        value = raw.get("next")
        if isinstance(value, int) and value >= floor:
            return value
    return floor


class IdAllocator:
    """一次 `sync` 里连续取号，每次取号立刻落盘（崩溃也不会重号）。"""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._next = read_next(ws)

    def allocate(self) -> str:
        entry_id = f"wf-{self._next:04d}"
        self._next += 1
        atomic_write_json(self.ws.ids_file, {"next": self._next})
        return entry_id

    def entry_dir(self, entry_id: str) -> Path:
        return self.ws.entry_dir(entry_id)
