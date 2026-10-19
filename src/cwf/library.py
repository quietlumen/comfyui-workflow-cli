"""登记、身份判定、变更检测（PRD §7 `cwf sync`）。

身份判定的顺序是**确定 → 确定 → 猜**：
1. path 命中（同一条目）
2. 旧路径消失 + sha256 相同 → moved
3. 旧路径消失 + 内容变了 → 指纹近邻（唯一高分才认）
4. 都不中 → new
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import CwfError, ExitCode
from .graph import (
    RULE_VERSION,
    classify_function,
    detect_format,
    fingerprint,
    similarity,
    summarize_nodes,
)
from .ids import IdAllocator
from .io_utils import atomic_write_json, read_json_or
from .ledger import Entry, iter_entries
from .paths import Workspace

FINGERPRINT_MIN = 0.85
NAME_MIN = 0.6


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def _skipped(name: str) -> bool:
    low = name.lower()
    return (
        not low.endswith(".json")
        or low.endswith(".json.bak")
        or low.startswith(".")
        or low.endswith(".tmp")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ScannedFile:
    path: Path
    rel_path: str
    data: bytes
    raw: Any
    sha256: str
    size: int
    mtime: str

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def fingerprint(self) -> Counter:
        return fingerprint(self.raw)


def scan(root: Path) -> list[ScannedFile]:
    files: list[ScannedFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if _skipped(fn):
                continue
            path = Path(dirpath) / fn
            try:
                data = path.read_bytes()
                stat = path.stat()
            except OSError:
                continue
            try:
                raw = __import__("json").loads(data.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                raw = None
            files.append(
                ScannedFile(
                    path=path,
                    rel_path=path.relative_to(root).as_posix(),
                    data=data,
                    raw=raw,
                    sha256=sha256_bytes(data),
                    size=stat.st_size,
                    mtime=_iso(stat.st_mtime),
                )
            )
    return files


@dataclass
class Change:
    id: str | None
    mark: str
    reason: str
    rel_path: str | None = None
    # None = 没带 --convert，没尝试过；False = 带了但这条不需要转；True = 真转了
    converted: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mark": self.mark,
            "reason": self.reason,
            "converted": self.converted,
        }


@dataclass
class Suspect:
    new_id: str
    candidates: list[str]
    best_j: float
    rel_path: str

    def to_json(self) -> dict[str, Any]:
        hint = ""
        if self.candidates:
            hint = f"cwf sync --adopt {self.new_id}={self.candidates[0]}"
        return {
            "new_id": self.new_id,
            "candidates": self.candidates,
            "best_J": round(self.best_j, 3),
            "rel_path": self.rel_path,
            "hint": hint,
        }


@dataclass
class SyncReport:
    scanned: int = 0
    changes: list[Change] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)
    ignored: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": True,
            "scanned": self.scanned,
            "changes": [c.to_json() for c in self.changes],
            "suspects": [s.to_json() for s in self.suspects],
            "ignored": self.ignored,
        }

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.changes:
            out[c.mark] = out.get(c.mark, 0) + 1
        return out


# --------------------------------------------------------------------------- 写盘


def _copy_source(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(src.read_bytes())
    os.replace(tmp, dst)


def _base_meta(entry_id: str, f: ScannedFile) -> dict[str, Any]:
    function, evidence = classify_function(f.raw)
    return {
        "id": entry_id,
        "name": f.stem,
        "aliases": [],
        "origin": "instance",
        "function": function,
        "function_source": "auto",
        "function_evidence": evidence,
        "function_rule": RULE_VERSION,
        "source": {
            "path": str(f.path),
            "rel_path": f.rel_path,
            "sha256": f.sha256,
            "size": f.size,
            "mtime": f.mtime,
            "present": True,
        },
        "format": detect_format(f.raw),
        "api_sha256": None,
        "api_origin": None,
        "api_at": None,
        "api_stale": False,
        "verified": False,
        "nodes": summarize_nodes(f.raw),
        "warnings": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def _refresh_facts(meta: dict[str, Any], f: ScannedFile) -> None:
    """只刷新"事实"字段；用户给的东西（别名、manual 分类）一律保留。"""
    meta["name"] = f.stem
    meta["source"] = {
        "path": str(f.path),
        "rel_path": f.rel_path,
        "sha256": f.sha256,
        "size": f.size,
        "mtime": f.mtime,
        "present": True,
    }
    meta["format"] = detect_format(f.raw)
    meta["nodes"] = summarize_nodes(f.raw)
    if meta.get("function_source") != "manual":
        function, evidence = classify_function(f.raw)
        meta["function"] = function
        meta["function_evidence"] = evidence
        meta["function_source"] = "auto"
        meta["function_rule"] = RULE_VERSION
    meta["updated_at"] = _now()


def needs_reclassify(meta: dict[str, Any]) -> bool:
    """规则升级后，auto 分类要重算（manual 的永远不动）。"""
    return (
        meta.get("function_source") != "manual"
        and meta.get("function_rule") != RULE_VERSION
    )


def refresh_source_from_disk(entry: Entry) -> bool:
    """转换前把 `source.json` 与实例文件对齐——文件可能在 `sync` 之后又被改过。

    为什么要对齐：旁路透传要靠源文件里的连线类型（`modes.UiTypes`），
    用一份过期的快照会让"透传到哪个输入"判断错。返回是否真的更新了。
    """
    source = entry.meta.get("source") or {}
    path = source.get("path")
    if not path:
        return False
    src = Path(str(path))
    try:
        data = src.read_bytes()
    except OSError:
        return False
    sha = sha256_bytes(data)
    if sha == str(source.get("sha256")):
        return False
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return False
    f = ScannedFile(
        path=src,
        rel_path=str(source.get("rel_path") or src.name),
        data=data,
        raw=raw,
        sha256=sha,
        size=len(data),
        mtime=_iso(src.stat().st_mtime),
    )
    _copy_source(src, entry.dir / "source.json")
    meta = dict(entry.meta)
    _refresh_facts(meta, f)
    atomic_write_json(entry.dir / "meta.json", meta)
    entry.meta = meta
    return True


def reclassify(ws: Workspace, entry: Entry) -> str:
    """就地重算一个条目的 auto 分类（不需要改源文件）。"""
    raw = read_json_or(entry.source_file, None) or read_json_or(entry.api_file, None)
    if not isinstance(raw, dict):
        return ""
    function, evidence = classify_function(raw)
    meta = dict(entry.meta)
    meta["function"] = function
    meta["function_evidence"] = evidence
    meta["function_source"] = "auto"
    meta["function_rule"] = RULE_VERSION
    meta["updated_at"] = _now()
    atomic_write_json(entry.dir / "meta.json", meta)
    return function


def _save_entry(ws: Workspace, entry_id: str, meta: dict[str, Any], f: ScannedFile) -> None:
    d = ws.entry_dir(entry_id)
    d.mkdir(parents=True, exist_ok=True)
    _copy_source(f.path, d / "source.json")
    atomic_write_json(d / "meta.json", meta)
    params = d / "params.json"
    if not params.exists():
        atomic_write_json(params, [])


# --------------------------------------------------------------------------- sync


def _name_evidence(old_stem: str, new_stem: str) -> bool:
    if old_stem == new_stem:
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(None, old_stem, new_stem).ratio() >= NAME_MIN


def sync(
    ws: Workspace,
    workflows_dir: Path,
    *,
    only: str | None = None,
    dry_run: bool = False,
    no_move: bool = False,
) -> SyncReport:
    """扫目录 → 判定身份 → 落库。`dry_run` 不写任何文件、不占 ID。"""
    report = SyncReport()
    if not workflows_dir.is_dir():
        raise CwfError(
            f"工作流目录不存在：{workflows_dir}",
            code=ExitCode.USAGE,
            error_code="NO_WORKFLOWS_DIR",
            hint="先跑 cwf init --instance-dir <ComfyUI 目录>",
        )

    files = scan(workflows_dir)
    report.scanned = len(files)

    entries = iter_entries(ws)
    inst = [e for e in entries if e.origin == "instance" and isinstance(e.meta.get("source"), dict)]
    by_rel: dict[str, Entry] = {
        str(e.meta["source"].get("rel_path")): e
        for e in inst
        if e.meta["source"].get("rel_path")
    }
    by_sha: dict[str, list[Entry]] = {}
    for e in inst:
        by_sha.setdefault(str(e.meta["source"].get("sha256")), []).append(e)

    scanned_rel = {f.rel_path for f in files}
    claimed: set[str] = set()
    allocator = IdAllocator(ws)

    def wanted(entry_id: str | None) -> bool:
        return only is None or entry_id == only

    for f in files:
        # 1) 路径命中
        hit = by_rel.get(f.rel_path)
        if hit is not None:
            claimed.add(hit.id)
            was_absent = hit.meta["source"].get("present", True) is False
            if str(hit.meta["source"].get("sha256")) == f.sha256:
                if was_absent and not dry_run and wanted(hit.id):
                    meta = dict(hit.meta)
                    meta["source"] = dict(meta["source"])
                    meta["source"]["present"] = True
                    meta["updated_at"] = _now()
                    atomic_write_json(hit.dir / "meta.json", meta)
                reason = "文件回来了，内容没变" if was_absent else "路径与内容都没变"
                if needs_reclassify(hit.meta):
                    if not dry_run and wanted(hit.id):
                        reclassify(ws, hit)
                    reason += "；按新的分类规则重算 function"
                report.changes.append(Change(hit.id, "unchanged", reason, f.rel_path))
                continue
            if not dry_run and wanted(hit.id):
                meta = dict(hit.meta)
                _refresh_facts(meta, f)
                if (hit.dir / "api.json").is_file():
                    meta["api_stale"] = True
                    meta["verified"] = False
                _save_entry(ws, hit.id, meta, f)
            report.changes.append(Change(hit.id, "changed", "同一路径，内容变了", f.rel_path))
            continue

        # 2) 内容命中（旧路径已消失）
        same_sha = [
            e
            for e in by_sha.get(f.sha256, [])
            if e.id not in claimed and str(e.meta["source"].get("rel_path")) not in scanned_rel
        ]
        if len(same_sha) == 1:
            old = same_sha[0]
            claimed.add(old.id)
            if not dry_run and wanted(old.id):
                meta = dict(old.meta)
                _refresh_facts(meta, f)
                _save_entry(ws, old.id, meta, f)
            report.changes.append(Change(old.id, "moved", "内容没变，位置/名字变了", f.rel_path))
            continue
        if len(same_sha) > 1:
            # 多个同内容条目都"消失"了：分不清谁是谁，不猜
            new_id = f"wf-{allocator._next:04d}" if dry_run else None
            report.suspects.append(
                Suspect(new_id or "(待分配)", [e.id for e in same_sha], 1.0, f.rel_path)
            )

        # 3) 指纹近邻（内容也变了）
        candidates = [
            e
            for e in inst
            if e.id not in claimed and str(e.meta["source"].get("rel_path")) not in scanned_rel
        ]
        scored: list[tuple[float, Entry, bool]] = []
        for e in candidates:
            old_raw = read_json_or(e.dir / "source.json", None)
            score = similarity(fingerprint(old_raw), f.fingerprint)
            if score >= FINGERPRINT_MIN:
                scored.append((score, e, _name_evidence(str(e.meta.get("name") or ""), f.stem)))
        scored.sort(key=lambda x: -x[0])

        matched: Entry | None = None
        if not no_move and scored:
            top_score, top_entry, top_name_ok = scored[0]
            if len(scored) == 1 and top_name_ok:
                matched = top_entry
            else:
                new_id = "(待分配)" if dry_run else ""
                report.suspects.append(
                    Suspect(
                        new_id or "(待分配)",
                        [e.id for _, e, _ in scored],
                        top_score,
                        f.rel_path,
                    )
                )

        if matched is not None:
            claimed.add(matched.id)
            if not dry_run and wanted(matched.id):
                meta = dict(matched.meta)
                _refresh_facts(meta, f)
                if (matched.dir / "api.json").is_file():
                    meta["api_stale"] = True
                    meta["verified"] = False
                _save_entry(ws, matched.id, meta, f)
            report.changes.append(
                Change(matched.id, "moved_changed", "位置变了、内容也变了（指纹唯一高分）", f.rel_path)
            )
            continue

        # 4) 新条目
        if dry_run:
            report.changes.append(Change("(待分配)", "new", "未登记过的文件", f.rel_path))
            continue
        dup = [
            e
            for e in inst
            if e.id not in claimed
            and str(e.meta["source"].get("sha256")) == f.sha256
        ]
        if dup:
            report.ignored.append({"rel_path": f.rel_path, "why": "duplicate"})
        entry_id = allocator.allocate()
        meta = _base_meta(entry_id, f)
        _save_entry(ws, entry_id, meta, f)
        report.changes.append(Change(entry_id, "new", "未登记过的文件", f.rel_path))

    # 收尾：旧路径消失的条目 → missing
    for e in inst:
        if e.id in claimed:
            continue
        rel = str(e.meta["source"].get("rel_path"))
        if rel in scanned_rel:
            continue                       # 路径还在，只是被前面的分支处理过了
        if not wanted(e.id):
            continue
        if e.meta["source"].get("present", True) is False:
            report.changes.append(Change(e.id, "missing", "源文件仍不在", rel))
            continue
        if not dry_run:
            meta = dict(e.meta)
            meta["source"] = dict(meta["source"])
            meta["source"]["present"] = False
            meta["updated_at"] = _now()
            atomic_write_json(e.dir / "meta.json", meta)
        report.changes.append(Change(e.id, "missing", "源文件不见了", rel))

    return report


# --------------------------------------------------------------------------- adopt


def adopt(ws: Workspace, new_id: str, old_id: str) -> str:
    """把 `new` 条目的文件接管给 `old`：保留 old 的参数绑定。"""
    entries = {e.id: e for e in iter_entries(ws)}
    if new_id not in entries:
        raise CwfError(f"找不到新条目：{new_id}", code=ExitCode.USAGE, error_code="NOT_FOUND")
    if old_id not in entries:
        raise CwfError(f"找不到旧条目：{old_id}", code=ExitCode.USAGE, error_code="NOT_FOUND")
    new, old = entries[new_id], entries[old_id]
    if new.origin != "instance" or old.origin != "instance":
        raise CwfError(
            "只有 origin: instance 的条目能互相接管",
            code=ExitCode.USAGE,
            error_code="ADOPT_UNSUPPORTED",
        )
    if new.params:
        raise CwfError(
            f"{new_id} 已经有 {len(new.params)} 个参数，不能接管",
            code=ExitCode.USAGE,
            error_code="ADOPT_CONFLICT",
            hint=f"先 cwf remove {new_id} --purge",
        )
    runs = new.dir / "runs"
    if runs.is_dir() and any(runs.iterdir()):
        raise CwfError(
            f"{new_id} 已经有运行记录，不能接管",
            code=ExitCode.USAGE,
            error_code="ADOPT_CONFLICT",
            hint=f"先 cwf remove {new_id} --purge",
        )

    meta = dict(old.meta)
    meta["source"] = dict(new.meta["source"])
    meta["name"] = new.meta.get("name") or meta.get("name")
    if meta.get("function_source") != "manual":
        meta["function"] = new.meta.get("function", meta.get("function"))
        meta["function_evidence"] = new.meta.get("function_evidence")
    meta["format"] = new.meta.get("format")
    meta["nodes"] = new.meta.get("nodes")
    meta["api_stale"] = (old.dir / "api.json").is_file()
    meta["verified"] = False
    meta["updated_at"] = _now()
    src = new.dir / "source.json"
    if src.is_file():
        _copy_source(src, old.dir / "source.json")
    atomic_write_json(old.dir / "meta.json", meta)

    import shutil

    shutil.rmtree(new.dir, ignore_errors=True)
    return f"{new_id} 的文件已接管到 {old_id}（参数保留 {len(old.params)} 个）"
