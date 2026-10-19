"""批量调用：三种驱动器 → 逐行调 `executor.run_once()`（PRD §7 FR-4）。"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

from .errors import usage

RESERVED = ("_id", "_tag", "_preset")


@dataclass
class Row:
    index: int
    params: dict[str, str] = field(default_factory=dict)
    _id: str = ""
    tag: str | None = None
    preset: str | None = None


def make_id(index: int, params: dict[str, str]) -> str:
    """`<行号>-<参数规范化 sha8>`：同一行改参数会换 _id，不会被误判成"已完成"。"""
    body = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return f"{index}-{digest}"


def _rows_from_dicts(dicts: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = []
    for i, raw in enumerate(dicts, start=1):
        params: dict[str, str] = {}
        for key, value in raw.items():
            name = str(key)
            if name in RESERVED or value is None:
                continue
            text = str(value).strip()
            if text == "":
                continue          # 空单元格 = 这一行不覆盖该参数
            params[name] = text
        explicit = str(raw.get("_id") or "").strip()
        rows.append(
            Row(
                index=i,
                params=params,
                _id=explicit or make_id(i, params),
                tag=(str(raw.get("_tag")).strip() or None) if raw.get("_tag") else None,
                preset=(str(raw.get("_preset")).strip() or None) if raw.get("_preset") else None,
            )
        )
    return rows


def from_csv(path: Path) -> list[Row]:
    if not path.is_file():
        raise usage(f"CSV 不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8-sig")   # 容忍 Excel 的 BOM
    except OSError as exc:
        raise usage(f"读不了 CSV：{exc}") from exc
    sample = text[:2048]
    if "\t" in sample and "," not in sample:
        dialect: Any = "excel-tab"
    else:
        dialect = "excel"
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        raise usage(f"CSV 没有表头：{path}", "第一行写参数名，例如 prompt,steps")
    unknown = [c for c in reader.fieldnames if c and c.startswith("_") and c not in RESERVED]
    if unknown:
        raise usage(f"CSV 里有不认识的保留列：{', '.join(unknown)}", "保留列只有 _id/_tag/_preset")
    return _rows_from_dicts(list(reader))


def from_jsonl(path: Path) -> list[Row]:
    if not path.is_file():
        raise usage(f"JSONL 不存在：{path}")
    dicts: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise usage(f"JSONL 第 {lineno} 行不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise usage(f"JSONL 第 {lineno} 行不是对象")
        dicts.append(raw)
    return _rows_from_dicts(dicts)


def from_matrix(spec: str) -> list[Row]:
    """`"k=a,b; k2=x,y"` → 笛卡尔积。"""
    axes: list[tuple[str, list[str]]] = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise usage(f"--matrix 的每段要写成 k=a,b，收到 {chunk!r}")
        key, values = chunk.split("=", 1)
        options = [v.strip() for v in values.split(",") if v.strip()]
        if not options:
            raise usage(f"--matrix 的 {key} 没有可选值")
        axes.append((key.strip(), options))
    if not axes:
        raise usage("--matrix 是空的", '例：--matrix "steps=4,8; cfg=1,2"')
    rows: list[Row] = []
    for i, combo in enumerate(product(*[values for _, values in axes]), start=1):
        params = {axes[j][0]: combo[j] for j in range(len(axes))}
        rows.append(Row(index=i, params=params, _id=make_id(i, params)))
    return rows


def parse_drivers(
    *, csv_path: str | None, jsonl_path: str | None, matrix: str | None
) -> tuple[str, list[Row]]:
    given = [x for x in (csv_path, jsonl_path, matrix) if x]
    if len(given) != 1:
        raise usage(
            "--csv / --jsonl / --matrix 必须且只能给一个",
            f"现在给了 {len(given)} 个",
        )
    if csv_path:
        return "csv", from_csv(Path(csv_path))
    if jsonl_path:
        return "jsonl", from_jsonl(Path(jsonl_path))
    assert matrix is not None
    return "matrix", from_matrix(matrix)


def summary_path(ws, ts: str) -> Path:
    return ws.batch_dir / f"{ts}.json"


def new_batch_id() -> str:
    return "b-" + datetime.now().strftime("%Y%m%d-%H%M")
