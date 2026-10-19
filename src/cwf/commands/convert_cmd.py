"""`cwf convert`（PRD §7）：UI→API，或直接导入 API 文件建条目。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer

from ..capture import Target, capture, load_api_file
from ..client import guard_remote
from ..config import load
from ..context import Ctx
from ..errors import CwfError, ExitCode, not_initialized, usage
from ..graph import classify_function, detect_format, summarize_nodes
from ..ids import IdAllocator
from ..io_utils import atomic_write_json, read_json_or
from ..ledger import Entry, iter_entries
from ..library import _now, refresh_source_from_disk
from ..modes import FULL_API_NAME
from ..output import payload
from ..parameters import relocate
from ..resolve import resolve


def _api_hash(api: dict) -> str:
    body = json.dumps(api, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _find_api_duplicate(entries: list[Entry], stem: str, api_sha: str) -> Entry | None:
    for e in entries:
        if e.origin != "api":
            continue
        if e.name == stem and str(e.meta.get("api_sha256")) == api_sha:
            return e
    return None


def _create_api_entry(ws, api: dict, stem: str) -> Entry:
    allocator = IdAllocator(ws)
    entry_id = allocator.allocate()
    d = ws.entry_dir(entry_id)
    d.mkdir(parents=True, exist_ok=True)
    function, evidence = classify_function(api)
    now = _now()
    meta = {
        "id": entry_id,
        "name": stem,
        "aliases": [],
        "origin": "api",
        "function": function,
        "function_source": "auto",
        "function_evidence": evidence,
        "function_rule": 0,          # 由 graph.RULE_VERSION 覆盖，见下
        "source": None,
        "format": detect_format(api),
        "api_sha256": _api_hash(api),
        "api_origin": "file",
        "api_at": now,
        "api_stale": False,
        "verified": False,
        "nodes": summarize_nodes(api),
        "warnings": [],
        "created_at": now,
        "updated_at": now,
    }
    from ..graph import RULE_VERSION

    meta["function_rule"] = RULE_VERSION
    atomic_write_json(d / "api.json", api)
    atomic_write_json(d / "meta.json", meta)
    atomic_write_json(d / "params.json", [])
    return Entry(id=entry_id, dir=d, meta=meta, params=[])


def _write_api(
    entry: Entry,
    api: dict,
    *,
    origin: str,
    drift: list[dict[str, str]],
    full: dict | None = None,
    skipped: list[str] | None = None,
    fallback: bool = False,
    api_fallback: bool = False,
) -> None:
    atomic_write_json(entry.dir / "api.json", api)
    full_file = entry.dir / FULL_API_NAME
    if full:
        atomic_write_json(full_file, full)
    elif full_file.is_file():
        full_file.unlink()
    meta = dict(entry.meta)
    meta["api_sha256"] = _api_hash(api)
    meta["api_origin"] = origin
    meta["api_at"] = _now()
    meta["api_stale"] = False
    meta["format"] = detect_format(api)
    nodes = summarize_nodes(api)
    # API 图不带 mode：旁路/静音的真实数字只能从源文件来（sync 时算过，这里再对齐一次）
    ui_nodes = summarize_nodes(read_json_or(entry.source_file, None))
    if ui_nodes["total"]:
        nodes["bypassed"] = ui_nodes["bypassed"]
        nodes["muted"] = ui_nodes["muted"]
    nodes["skipped"] = sorted({str(x) for x in (skipped or [])},
                              key=lambda s: (int(s) if s.isdigit() else 10**9, s))
    nodes["full_total"] = len(full) if full else len(api)
    if fallback:
        # api.json 已经是"全部启用"的图，里面没有被旁路的节点了，所以 skip 集合必须清空：
        # 留着它们会让 build_payload 以为"这些能重新打开"，一 --enable 就把别的全裁掉。
        # 源文件里到底旁路了多少，另记一笔供人看。
        nodes["bypassed_in_source"] = nodes["bypassed"]
        nodes["bypassed"] = 0
        nodes["skipped"] = []
    meta["nodes"] = nodes
    meta["updated_at"] = _now()
    meta["api_fallback"] = bool(fallback or api_fallback)
    # 重复 convert 不能把同一条警告堆成好几条
    marks = {"api_fallback"} | ({"param_drift"} if drift else set())
    warnings = [w for w in (meta.get("warnings") or []) if (w or {}).get("kind") not in marks]
    if fallback:
        warnings.append({
            "kind": "api_fallback",
            "detail": "照文件状态导不出图（整个流都被旁路了），api.json 用的是全部启用后的完整图",
        })
    elif api_fallback:
        warnings.append({
            "kind": "api_fallback",
            "detail": "源文件本身就是 API 格式，直接收录，未经浏览器转换",
        })
    if drift:
        # 漂移报告要能被 `inspect --warnings` / `show` 看见，所以必须进 meta.json
        # **在落盘之前**追加——否则只留在内存里，写出去的那份没有这条。
        warnings.append({"kind": "param_drift", "detail": f"{len(drift)} 个参数重新定位"})
    meta["warnings"] = warnings
    atomic_write_json(entry.dir / "meta.json", meta)
    entry.meta = meta


def run(
    ctx: Ctx,
    *,
    target: str | None,
    all_: bool,
    visible: bool,
    file: str | None,
    force: bool,
    timeout: int | None,
    eval_timeout: int | None,
    browser: str | None,
    browser_exe: str | None,
    allow_remote: bool,
) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    cfg = load(ws)
    guard_remote(cfg, allow=allow_remote, command="convert")
    if browser and browser not in ("chromium", "chrome", "msedge"):
        raise usage("--browser 只能是 chromium | chrome | msedge")
    entries = iter_entries(ws)
    opts = {
        "nav_timeout": timeout,
        "eval_timeout": eval_timeout,
        "browser": browser,
        "browser_exe": browser_exe,
    }

    # ---- 情形一：--file 且没给 <ID> → 新建 api 条目 ----
    if file and not target and not all_:
        path = Path(file)
        api = load_api_file(path)
        stem = path.stem
        dup = _find_api_duplicate(entries, stem, _api_hash(api))
        if dup is not None:
            raise CwfError(
                f"已经导入过同样的文件：{dup.id}（{dup.name}）",
                code=ExitCode.USAGE,
                error_code="DUPLICATE_IMPORT",
                hint=f"要更新就用 cwf convert {dup.id} --file \"{file}\"",
            )
        entry = _create_api_entry(ws, api, stem)
        data = payload(
            id=entry.id, name=entry.name, origin="api", api_sha256=entry.meta["api_sha256"],
            function=entry.meta["function"], created=True, drift=[],
        )

        def human() -> None:
            ctx.out.line(f"[green]✓[/green] 新建条目 {entry.id}（{entry.name}）")
            ctx.out.line(f"  节点 {entry.meta['nodes'].get('total')} 个 · 功能 {entry.meta['function']}"
                         f"（{entry.meta['function_evidence']}）")
            ctx.out.line("  下一步：cwf inspect " + entry.id + " --suggestion")

        ctx.out.emit(data, human)
        return

    # ---- 情形二：给已有条目换 API 图 ----
    if target and not all_:
        entry = resolve(entries, target)
        if entry.origin == "api" and not file:
            raise CwfError(
                f"{entry.id} 是导入的 API 条目，没有源文件可加载",
                code=ExitCode.USAGE,
                error_code="NO_SOURCE",
                hint=f"用 cwf convert {entry.id} --file <x_api.json> 更新",
            )
        if file:
            api = load_api_file(Path(file))
            drift = relocate(ws, entry.dir, entry.params, api)
            _write_api(entry, api, origin="file", drift=drift)
            _emit_one(ctx, entry, drift, created=False, origin="file")
            return
        if entry.state == "missing":
            raise CwfError(
                f"{entry.id} 的源文件不在了，无法自动转换",
                code=ExitCode.USAGE,
                error_code="NO_SOURCE",
                hint="把文件放回实例目录后 cwf sync，或用 --file 导入",
            )
        if not force and not entry.meta.get("api_stale") and entry.api_file.is_file():
            ctx.out.emit(
                payload(id=entry.id, name=entry.name, skipped=True, reason="api.json 已是最新"),
                lambda: ctx.out.line(f"{entry.id} 的 api.json 已是最新（--force 可强制重转）"),
            )
            return
        if entry.meta.get("source") is None:
            raise usage(f"{entry.id} 没有源文件", "用 --file 导入 API 图")
        done, failures, refreshed = _capture_and_write(
            ctx, cfg, [entry], visible=visible, **opts
        )
        _emit_convert(ctx, done, failures, refreshed)
        return

    # ---- 情形三：--all ----
    if all_:
        targets = [
            e
            for e in entries
            if e.origin == "instance"
            and (e.meta.get("source") or {}).get("present", True)
            and (force or e.state in ("registered", "stale", "changed"))
        ]
        if not targets:
            ctx.out.emit(payload(converted=[], failed=[]), lambda: ctx.out.line("没有需要转换的条目。"))
            return
        done, failures, refreshed = _capture_and_write(
            ctx, cfg, targets, visible=visible, **opts
        )
        _emit_convert(ctx, done, failures, refreshed)
        return

    raise usage("给一个 <ID>，或 --all，或用 --file 建条目")


def _emit_one(ctx: Ctx, entry: Entry, drift: list[dict[str, str]], *, created: bool,
              origin: str) -> None:
    data = payload(
        id=entry.id,
        name=entry.name,
        origin=entry.origin,
        api_sha256=entry.meta.get("api_sha256"),
        api_origin=origin,
        function=entry.meta.get("function"),
        nodes=(entry.meta.get("nodes") or {}).get("total"),
        created=created,
        drift=drift,
    )

    def human() -> None:
        out = ctx.out
        out.line(f"[green]✓[/green] {entry.id}（{entry.name}）的 api.json 已更新"
                 f"（{origin}，{data['nodes']} 个节点）")
        for d in drift:
            out.line(f"  [yellow]参数漂移[/yellow] {d['name']}: 节点 {d['from']} → {d['to']}")
        out.line(f"  下一步：cwf inspect {entry.id} --suggestion")

    ctx.out.emit(data, human)


def _capture_and_write(
    ctx: Ctx, cfg, entries: list[Entry], *, visible: bool,
    nav_timeout: int | None = None, eval_timeout: int | None = None,
    browser: str | None = None, browser_exe: str | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    targets: list[Target] = []
    refreshed: list[str] = []
    # 源文件本身就是 API 格式（实例目录里躺着一份导出过的 API JSON）：不用起浏览器
    already_api: list[tuple[Entry, dict]] = []
    for e in entries:
        # 文件可能在 sync 之后又被改过：先对齐快照，否则旁路透传会用过期类型
        if refresh_source_from_disk(e):
            refreshed.append(e.id)
        source = e.meta.get("source") or {}
        rel = source.get("rel_path")
        if not rel:
            continue
        raw = read_json_or(e.source_file, None)
        if detect_format(raw) == "api":
            try:
                already_api.append((e, load_api_file(e.source_file)))
            except CwfError:
                # 结构不合格就交给浏览器做（至少还能报出真实原因）
                targets.append(Target(entry_id=e.id, rel_path=str(rel), source_json=e.source_file))
            continue
        targets.append(Target(entry_id=e.id, rel_path=str(rel), source_json=e.source_file))

    strict = len(targets) + len(already_api) == 1
    results, failures = capture(
        cfg, targets, visible=visible, strict=strict, tmp_dir=ctx.ws.cache_dir / "tmp",
        nav_timeout=nav_timeout, eval_timeout=eval_timeout,
        browser=browser, browser_exe=browser_exe,
    )
    by_id = {e.id: e for e in entries}
    done: list[dict] = []
    for entry, raw in already_api:
        drift = relocate(ctx.ws, entry.dir, entry.params, raw)
        _write_api(entry, raw, origin="file", drift=drift, api_fallback=True)
        done.append({
            "id": entry.id, "name": entry.name, "drift": drift,
            "nodes": len(raw), "full_nodes": len(raw), "skipped": [],
            "fallback": False, "source_api": True,
        })
    for captured in results:
        entry = by_id[captured.target.entry_id]
        canonical = captured.full or captured.api
        drift = relocate(ctx.ws, entry.dir, entry.params, canonical)
        _write_api(
            entry, captured.api, origin="capture", drift=drift,
            full=captured.full, skipped=captured.skipped,
            fallback=captured.fallback, api_fallback=False,
        )
        done.append({
            "id": entry.id, "name": entry.name, "drift": drift,
            "nodes": len(captured.api), "full_nodes": len(canonical),
            "skipped": captured.skipped, "fallback": captured.fallback,
            "source_api": False,
        })

    return done, failures, refreshed


def _emit_convert(
    ctx: Ctx, done: list[dict], failures: list[dict], refreshed: list[str]
) -> None:
    data = payload(converted=done, failed=failures)

    def human() -> None:
        out = ctx.out
        if refreshed:
            out.line(f"[dim]源文件有更新，已同步快照：{', '.join(refreshed)}[/dim]")
        for item in done:
            out.line(f"[green]✓[/green] {item['id']}（{item['name']}）转换完成")
            if item.get("source_api"):
                out.line("  [dim]源文件本身就是 API 格式，直接收录（没经过浏览器）[/dim]")
            elif item.get("fallback"):
                out.line(
                    "  [yellow]注意[/yellow] 整个流都被旁路了，照文件状态导不出图；"
                    "api.json 用的是全部启用后的完整图"
                )
            if item["skipped"]:
                out.line(
                    f"  [dim]旁路 {len(item['skipped'])} 个节点：{', '.join(item['skipped'])}"
                    f" → 完整图 {item['full_nodes']} 个节点（api.full.json）[/dim]"
                )
            for d in item["drift"]:
                out.line(f"  [yellow]参数漂移[/yellow] {d['name']}: 节点 {d['from']} → {d['to']}")
        for f in failures:
            out.line(f"[red]✗[/red] {f['id']} 失败：{f['error']}")
        if done:
            out.line("")
            out.line("下一步：cwf inspect <ID> --suggestion 看能绑哪些参数")

    ctx.out.emit(data, human)
    if failures and not done:
        raise CwfError(
            f"{len(failures)} 个条目全部转换失败",
            code=ExitCode.ERROR,
            error_code="CONVERT_FAILED",
            hint="加 --visible 看浏览器里发生了什么，或用 --file 导入",
        )


def register(app: typer.Typer) -> None:
    @app.command("convert")
    def convert_cmd(
        ctx: typer.Context,
        target: str | None = typer.Argument(None, help="ID / 别名 / 名字 / 唯一前缀"),
        all_: bool = typer.Option(False, "--all", help="转换所有待转换的条目"),
        visible: bool = typer.Option(False, "--visible", help="有头模式（排查无头崩溃）"),
        file: str | None = typer.Option(None, "--file", help="导入人工导出的 API JSON"),
        force: bool = typer.Option(False, "--force", help="api.json 已是最新也强制重转"),
        timeout: int | None = typer.Option(None, "--timeout", help="导航与就绪超时（秒）"),
        eval_timeout: int | None = typer.Option(
            None, "--eval-timeout", help="单次 graphToPrompt 求值超时（秒）"
        ),
        browser: str | None = typer.Option(
            None, "--browser", help="chromium | chrome | msedge"
        ),
        browser_exe: str | None = typer.Option(
            None, "--browser-exe", help="完全指定可执行文件"
        ),
        allow_remote: bool = typer.Option(
            False, "--allow-remote", help="允许连非本机实例取工作流"
        ),
    ) -> None:
        """UI→API 转换；`--file` 且不给 <ID> 时直接建一个无源文件的条目。"""
        run(
            ctx.obj,
            target=target,
            all_=all_,
            visible=visible,
            file=file,
            force=force,
            timeout=timeout,
            eval_timeout=eval_timeout,
            browser=browser,
            browser_exe=browser_exe,
            allow_remote=allow_remote,
        )
