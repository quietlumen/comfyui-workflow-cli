"""提交与执行：**唯一的提交路径** `run_once()`（附录 §5 硬约束）。

`run` / `batch` / `queue` 都调它，所以运行记录天然一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import client, logstore
from .config import Config
from .context import Ctx
from .errors import CwfError, ExitCode
from .graph import is_save_node
from .io_utils import read_json_or
from .ledger import Entry
from .modes import build_payload, load_canonical, load_full, skipped_ids
from .parameters import apply_overrides, apply_values, local_file, merge_values
from .paths import Workspace

SAVE_NODES = ("SaveImage", "SaveAnimatedWEBP", "SaveVideo", "VHS_VideoCombine", "SaveWEBM")


@dataclass
class Prepared:
    payload: dict[str, Any]
    values: dict[str, Any]
    sources: dict[str, str]
    orphans: list[str] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    mode: dict[str, Any] = field(default_factory=dict)
    uploads: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunResult:
    run_id: str
    prompt_id: str | None
    status: str
    duration_seconds: float
    artifacts: list[dict[str, Any]]
    record: dict[str, Any]
    error: str | None = None
    node_errors: dict[str, Any] = field(default_factory=dict)


def load_presets(entry: Entry) -> dict[str, Any]:
    raw = read_json_or(entry.dir / "presets.json", {})
    return raw if isinstance(raw, dict) else {}


def prepare(
    entry: Entry,
    *,
    cli_params: dict[str, str] | None = None,
    presets: list[str] | None = None,
    overrides: list[str] | None = None,
    random_seed: bool = False,
    prefix: str | None = None,
    cfg: Config | None = None,
    enable: list[str] | None = None,
    skip: list[str] | None = None,
) -> Prepared:
    canonical = load_canonical(entry)
    if not canonical:
        raise CwfError(
            f"{entry.id} 还没有 api.json（状态 {entry.state}）",
            code=ExitCode.USAGE,
            error_code="NO_API",
            hint=f"先 cwf convert {entry.id}",
        )

    values, sources = merge_values(
        entry.params,
        load_presets(entry),
        list(presets or []),
        dict(cli_params or {}),
        random_seed=random_seed,
    )

    # 自动启用：绑在旁路节点上的参数，只要你这次显式给了值，那个节点就必须打开。
    # （`qwen_image_2.1_图像编辑` 用"开几个 LoadImage"表示"传几张图"，就是这个机制。）
    skipped = skipped_ids(entry.meta)
    auto_enable: list[str] = []
    for param in entry.params:
        name = str(param.get("name"))
        if sources.get(name) in (None, "default"):
            continue
        value = values.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        node_id = str(param.get("node") or "")
        if node_id and node_id in skipped:
            auto_enable.append(node_id)

    want_on = {str(x) for x in (enable or ())} | set(auto_enable)

    # 图片参数：**没给值就当作"这张图不存在"**（把它的节点旁路掉）。
    # 图生图/图生视频流的通用约定——默认是"没图"，给了才是"有图"。
    # 明确 `--enable` 的节点不受这条影响（用户说了要开就是开）。
    explicit = {k for k, src in sources.items() if src not in (None, "default")}
    auto_skip: list[str] = []
    for param in entry.params:
        if str(param.get("type")) not in ("image", "file"):
            continue
        if str(param.get("name")) in explicit:
            continue
        node_id = str(param.get("node") or "")
        if node_id:
            auto_skip.append(node_id)

    want_skip = ({str(x) for x in (skip or ())} | set(auto_skip)) - want_on
    payload, mode_info = build_payload(
        entry,
        enable=want_on,
        skip=want_skip,
        ui=_read_ui(entry),
    )
    key = lambda s: (int(s) if s.isdigit() else 10**9, s)  # noqa: E731
    mode_info["auto_enabled"] = sorted(set(auto_enable), key=key)
    mode_info["auto_skipped"] = sorted(set(auto_skip), key=key)
    # 显式 --enable 压过了"图片没给值就旁路"的节点：得让人看见这条规则被推翻了
    forced = set(str(x) for x in (enable or ())) & set(auto_skip)
    if forced:
        mode_info["forced"] = sorted(forced, key=key)
    if not (load_full(entry) or not skipped):
        mode_info["note"] = "没有 api.full.json：旁路节点打不开，cwf convert <ID> --force 可补"

    uploads: list[dict[str, Any]] = []
    for param in entry.params:
        name = str(param.get("name"))
        if str(param.get("type")) not in ("image", "file"):
            continue
        path = local_file(values.get(name))
        if path is None:
            continue
        if cfg is None:
            uploads.append({"param": name, "local_path": path, "name": None, "uploaded": False})
            continue
        info = client.upload_image(cfg, Path(path))
        values[name] = info.get("name") or Path(path).name
        sources[name] = f"{sources.get(name)}+upload"
        uploads.append(
            {"param": name, "local_path": path, "name": values[name], "uploaded": True}
        )

    orphans = apply_values(payload, entry.params, values, canonical=canonical)
    applied = apply_overrides(payload, list(overrides or []))

    if prefix:
        for node in payload.values():
            if isinstance(node, dict) and is_save_node(str(node.get("class_type") or "")):
                inputs = node.setdefault("inputs", {})
                if isinstance(inputs, dict):
                    inputs["filename_prefix"] = prefix

    return Prepared(
        payload=payload,
        values=values,
        sources=sources,
        orphans=orphans,
        overrides=applied,
        mode=mode_info,
        uploads=uploads,
    )


def _read_ui(entry: Entry) -> dict[str, Any] | None:
    """源文件（旁路透传需要节点类型）；读不到就返回 None。"""
    raw = read_json_or(entry.source_file, None)
    return raw if isinstance(raw, dict) and raw.get("nodes") else None


def _is_finished(history: dict[str, Any]) -> bool:
    """`/history/<id>` 里出现条目基本就等于跑完了；这里再做一次显式确认。"""
    status = history.get("status")
    if isinstance(status, dict):
        if status.get("completed") or status.get("status_str") in ("success", "error"):
            return True
    return bool(history.get("outputs"))


def _collect_items(history: dict[str, Any]) -> list[dict[str, Any]]:
    """从 `/history` 的 outputs 里挑出产物。

    工作流里常有 PreviewImage / ImageCompare 这类节点，它们也会产出 `ComfyUI_temp_*`
    临时图。**只要正式输出（`type == "output"`）存在，就不下载临时图**；
    一个正式输出都没有时才退回去拿全部（纯预览工作流）。
    """
    items: list[dict[str, Any]] = []
    outputs = history.get("outputs")
    if not isinstance(outputs, dict):
        return items
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs", "videos", "audio", "files"):
            for item in node_out.get(key) or []:
                if isinstance(item, dict) and item.get("filename"):
                    items.append(item)
    formal = [i for i in items if i.get("type") == "output"]
    return formal or items


def _kind(filename: str) -> str:
    low = filename.lower()
    if low.endswith((".mp4", ".webm", ".mkv")):
        return "video"
    if low.endswith((".gif", ".webp")):
        return "animation"
    if low.endswith((".wav", ".mp3", ".flac")):
        return "audio"
    return "image"


def run_once(
    ctx: Ctx,
    cfg: Config,
    entry: Entry,
    *,
    cli_params: dict[str, str] | None = None,
    presets: list[str] | None = None,
    overrides: list[str] | None = None,
    random_seed: bool = False,
    prefix: str | None = None,
    out_dir: str | None = None,
    timeout: int | None = None,
    wait: bool = True,
    dry_run: bool = False,
    client_id: str = "cwf",
    enable: list[str] | None = None,
    skip: list[str] | None = None,
    batch_meta: dict[str, Any] | None = None,
    job_meta: dict[str, Any] | None = None,
    tag: str | None = None,
    prepared: Prepared | None = None,
) -> RunResult | Prepared:
    """组包 → 提交 → 轮询 → 下载 → 写运行记录。`dry_run` 只返回组好的包。

    `batch_meta` / `job_meta` / `tag` 是**谁在调我**的凭据（附录 §6）：`batch` 传
    `{"_id": 行号-哈希}`，`queue` 传 `{"job_id", "attempt"}`，写进同一份记录的对应块。
    没有它们，`--resume` 找不到"这行跑过没有"，`queue retry` 也说不清第几次。

    `prepared` 让调用方把 `prepare()` 的结果直接递进来（已经付过上传的代价时别再付一遍）。
    """
    if prepared is None:
        prepared = prepare(
            entry,
            cli_params=cli_params,
            presets=presets,
            overrides=overrides,
            random_seed=random_seed,
            prefix=prefix,
            cfg=cfg,
            enable=enable,
            skip=skip,
        )
    if dry_run:
        return prepared

    ws: Workspace = ctx.ws
    # 注意：`--timeout 0` 是合法输入（当作"立刻超时"），不能用 `or` 兜底
    limit = int(timeout if timeout is not None else cfg.run.default_timeout)
    started = time.time()
    prompt_id = client.post_prompt(cfg, prepared.payload, client_id=client_id)
    run_id = logstore.make_run_id(prompt_id)

    base_record: dict[str, Any] = {
        "run_id": run_id,
        "workflow_id": entry.id,
        "workflow_name": entry.name,
        "prompt_id": prompt_id,
        "client_id": client_id,
        "api_sha256": entry.meta.get("api_sha256"),
        "params": {
            k: v for k, v in prepared.values.items()
            if prepared.sources.get(k) != "default"
        },
        "param_sources": prepared.sources,
        "status": "running" if not wait else "success",
        "queued_at": _iso(started),
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "artifacts": [],
        "submitted_prompt": prepared.payload,
        "error": None,
        "node_errors": {},
        "overrides": prepared.overrides,
        "orphans": prepared.orphans,
        "mode": prepared.mode,
        "uploads": prepared.uploads,
    }
    if batch_meta:
        base_record["batch"] = dict(batch_meta)
    if job_meta:
        base_record["job"] = dict(job_meta)
    if tag:
        base_record["tag"] = tag
    logstore.ensure_dir(ws, entry.id)
    logstore.write(ws, entry.id, base_record)

    if not wait:
        return RunResult(
            run_id=run_id,
            prompt_id=prompt_id,
            status="running",
            duration_seconds=0.0,
            artifacts=[],
            record=base_record,
        )

    return _await(
        cfg, ws, entry,
        run_id=run_id, prompt_id=prompt_id, base_record=base_record,
        limit=limit, out_dir=out_dir, started=started,
    )


def _await(
    cfg: Config,
    ws: Workspace,
    entry: Entry,
    *,
    run_id: str,
    prompt_id: str,
    base_record: dict[str, Any],
    limit: int,
    out_dir: str | None,
    started: float,
    deadline: float | None = None,
) -> RunResult:
    """轮询 `/history` 直到结束 → 下载产物 → 定稿记录。`--no-wait` 的补全也走这里。

    `started` 只用于算时长（补全时它就是原始的 `queued_at`）；真正等多久由
    `deadline` 决定——补全一份十分钟前提交的记录时，不该因为"已经超过 timeout"就秒退。
    """
    history = None
    deadline = deadline if deadline is not None else started + limit
    interval = max(0.3, float(cfg.run.wait_poll_interval))
    while time.time() < deadline:
        history = client.get_history(cfg, prompt_id)
        if history is not None and _is_finished(history):
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    duration = time.time() - started

    status = "success"
    error: str | None = None
    node_errors: dict[str, Any] = {}
    artifacts: list[dict[str, Any]] = []
    if history is None:
        status = "timeout"
        error = f"等待 {limit}s 仍未完成"
    else:
        status_info = history.get("status") or {}
        if isinstance(status_info, dict) and status_info.get("status_str") == "error":
            status = "failed"
            for msg in status_info.get("messages") or []:
                if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
                    detail = msg[1] if isinstance(msg[1], dict) else {}
                    error = str(detail.get("exception_message") or "执行出错")
                    node_errors = detail.get("node_errors") or {}
                    break
            if error is None:
                error = "执行出错（ComfyUI 未给出细节）"
        if status == "success":
            artifacts = _download(cfg, ws, entry, run_id, history, out_dir, prompt_id)

    record = dict(base_record)
    record.update(
        {
            "status": status,
            "started_at": _iso(started),
            "finished_at": _iso(started + duration),
            "duration_seconds": round(duration, 2),
            "artifacts": artifacts,
            "error": error,
            "node_errors": node_errors,
        }
    )
    logstore.write(ws, entry.id, record)
    if status == "success" and not entry.meta.get("verified"):
        _mark_verified(entry)

    if status == "failed":
        raise CwfError(
            f"执行失败：{error}",
            code=ExitCode.EXEC_FAILED,
            error_code="EXEC_FAILED",
            node_errors=node_errors,
            hint=f"cwf log {run_id} 看完整记录",
        )
    if status == "timeout":
        raise CwfError(
            f"等了 {limit}s 还没跑完（run_id={run_id}）",
            code=ExitCode.EXEC_FAILED,
            error_code="TIMEOUT",
            hint=f"cwf log {run_id}；或把 --timeout 调大",
        )

    return RunResult(
        run_id=run_id,
        prompt_id=prompt_id,
        status=status,
        duration_seconds=round(duration, 2),
        artifacts=artifacts,
        record=record,
    )


def complete_no_wait(
    ctx: Ctx,
    cfg: Config,
    entry: Entry,
    run_id: str,
    *,
    timeout: int | None = None,
    out_dir: str | None = None,
) -> RunResult:
    """补全一份 `status="running"` 的记录（附录 §6）：`batch --concurrency` 与 `cwf log` 共用。

    记录里已有 `prompt_id`，所以不需要重新提交——只是接着轮询到结束。
    `queued_at` 是原始的提交时刻，用它算时长，补出来的 duration 才和真跑一趟一致。
    """
    record = logstore.read(ctx.ws, entry.id, run_id)
    if record is None:
        raise CwfError(
            f"找不到运行记录 {run_id}",
            code=ExitCode.USAGE,
            error_code="NO_RECORD",
            hint=f"cwf history {entry.id} 看有哪些",
        )
    prompt_id = record.get("prompt_id")
    if not prompt_id:
        raise CwfError(
            f"{run_id} 没有 prompt_id，无法补全",
            code=ExitCode.ERROR,
            error_code="NO_PROMPT_ID",
        )
    limit = int(timeout if timeout is not None else cfg.run.default_timeout)
    started = _parse_iso(record.get("queued_at")) or time.time()
    return _await(
        cfg, ctx.ws, entry,
        run_id=run_id, prompt_id=str(prompt_id), base_record=record,
        limit=limit, out_dir=out_dir, started=started,
        deadline=time.time() + limit,       # 从现在开始等，不按原提交时刻算
    )


def _download(
    cfg: Config,
    ws: Workspace,
    entry: Entry,
    run_id: str,
    history: dict[str, Any],
    out_dir: str | None,
    prompt_id: str,
) -> list[dict[str, Any]]:
    base = Path(out_dir) if out_dir else ws.outputs_dir / entry.id / run_id
    base.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for item in _collect_items(history):
        filename = str(item.get("filename"))
        try:
            data = client.download_output(cfg, item)
        except CwfError as exc:
            artifacts.append(
                {"type": _kind(filename), "instance_path": filename,
                 "local_path": None, "error": exc.message}
            )
            continue
        local = base / filename
        logstore.write_atomic_bytes(local, data)
        entry_art: dict[str, Any] = {
            "type": _kind(filename),
            "instance_path": filename,
            "local_path": str(local),
            "bytes": len(data),
        }
        if filename.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp")):
            entry_art.update(_image_size(data))
        artifacts.append(entry_art)
    return artifacts


def _image_size(data: bytes) -> dict[str, int]:
    """从文件头读宽高（附录 §10.3）：PNG / JPEG / WebP 三种，认不出就返回空。"""
    size = _png_size(data)
    if size:
        return size
    size = _jpeg_size(data)
    if size:
        return size
    return _webp_size(data)


def _png_size(data: bytes) -> dict[str, int]:
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return {"width": width, "height": height}
    return {}


def _jpeg_size(data: bytes) -> dict[str, int]:
    """顺着 SOFn 段找尺寸。段长是大端、含自身这 2 字节，所以下一段从 +len 开始。"""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return {}
    i = 2
    end = len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1                       # 有些写入器会塞 0xFF 填充，跳过
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2                       # 无载荷的标记
            continue
        if marker == 0xD9:               # EOI：还没见到 SOFn
            return {}
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return {}
        # SOF0~SOF15，但排除 DHT(C4) / JPG(C8) / DAC(CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            if width and height:
                return {"width": width, "height": height}
            return {}
        i += 2 + seg_len
    return {}


def _webp_size(data: bytes) -> dict[str, int]:
    """RIFF/WebP 三种子格式：VP8（有损）/ VP8L（无损）/ VP8X（扩展）。"""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return {}
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return {"width": width, "height": height}
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return {}
        bits = int.from_bytes(data[21:25], "little")
        return {"width": (bits & 0x3FFF) + 1, "height": ((bits >> 14) & 0x3FFF) + 1}
    if chunk == b"VP8 ":
        # 关键帧起始码 9D 01 2A 之后 2 字节宽、2 字节高（各 14 位有效）
        idx = data.find(b"\x9d\x01\x2a", 20, 40)
        if idx < 0:
            return {}
        width = int.from_bytes(data[idx + 3:idx + 5], "little") & 0x3FFF
        height = int.from_bytes(data[idx + 5:idx + 7], "little") & 0x3FFF
        return {"width": width, "height": height}
    return {}


def _mark_verified(entry: Entry) -> None:
    from .io_utils import atomic_write_json

    meta = dict(entry.meta)
    meta["verified"] = True
    atomic_write_json(entry.dir / "meta.json", meta)
    entry.meta = meta


def _iso(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(text: Any) -> float | None:
    """`queued_at` 之类的时间戳 → epoch 秒；解析不了就返回 None（由调用方兜底）。"""
    from datetime import datetime

    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.strptime(text.strip()[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def dry_run_table(prepared: Prepared, entry: Entry) -> list[tuple[str, str, str]]:
    """(参数名, 值, 来源) —— 给 `--dry-run` 的人读表用。"""
    rows: list[tuple[str, str, str]] = []
    for p in entry.params:
        name = str(p.get("name"))
        value = prepared.values.get(name)
        rows.append((name, _short(value), prepared.sources.get(name, "default")))
    for item in prepared.overrides:
        rows.append((f"--set {item['node']}.{item['input']}", _short(item["value"]), "override"))
    return rows


def _short(value: Any, limit: int = 42) -> str:
    if value is None:
        return "null"
    text = str(value).replace("\n", " ")
    return text[:limit] + "…" if len(text) > limit else text
