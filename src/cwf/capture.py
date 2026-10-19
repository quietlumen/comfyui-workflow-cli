"""UI→API 转换：Playwright 驱动浏览器取 `graphToPrompt()`（附录 §10.2）。

**playwright 只在函数内 import**：`list`/`show`/`history` 的冷启动不能被它拖累。
**结果只走 `evaluate` 的返回值**：绝不让页面去 fetch 本地端口（Chrome 的 Private
Network Access 策略会拦掉）。
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import CwfError, ExitCode


class CaptureError(CwfError):
    def __init__(self, message: str, *, hint: str | None = None, code: ExitCode = ExitCode.ERROR):
        super().__init__(message, code=code, error_code="CONVERT_FAILED", hint=hint)


@dataclass
class Target:
    """要转换的一个工作流。"""

    entry_id: str
    rel_path: str
    source_json: Path


@dataclass
class Captured:
    """一次转换的两个产物：照文件旁路状态导出的图 + 全部启用后的完整图。"""

    target: Target
    api: dict[str, Any]
    full: dict[str, Any] | None
    skipped: list[str]
    # 照文件状态导不出图、只能拿完整图顶上（整个流都被旁路掉了）
    fallback: bool = False


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise CaptureError(
            "没装 playwright，无法自动转换",
            hint="pip install playwright && python -m playwright install chromium",
            code=ExitCode.USAGE,
        ) from exc
    return sync_playwright


def ensure_writable_temp(preferred: Path | None = None) -> Path:
    """把 TMP/TEMP/TMPDIR 指到一个**确定可写**的目录，再交给 Playwright。

    为什么要无条件重定向、而不是先探测系统临时目录：
    某些受限环境里对 `%TEMP%` 调 `mkdtemp` **不是报错而是卡住**（实测会一直等），
    探测本身就会把命令挂死。工作区里的 `cache/tmp` 是我们自己建的，一定可写。
    """
    import os
    import tempfile

    candidates = [
        preferred,
        Path(os.environ.get("LOCALAPPDATA") or "") / "cwf" / "tmp",
        Path.home() / ".cwf" / "tmp",
    ]
    for cand in candidates:
        if cand is None:
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for var in ("TMP", "TEMP", "TMPDIR"):
            os.environ[var] = str(cand)
        tempfile.tempdir = str(cand)
        return cand
    raise CaptureError(
        "找不到可写的临时目录，浏览器起不来",
        hint="把 TEMP 指到一个可写目录再试",
    )


def _launch(p, cfg: Config, *, visible: bool):
    cap = cfg.capture
    kwargs: dict[str, Any] = {"headless": (not visible) and cap.headless}
    if cap.browser_exe:
        kwargs["executable_path"] = cap.browser_exe
    elif cap.browser in ("chrome", "msedge"):
        kwargs["channel"] = cap.browser
    if cap.launch_args:
        kwargs["args"] = list(cap.launch_args)
    try:
        return p.chromium.launch(**kwargs)
    except Exception as exc:  # pragma: no cover - 依赖真实浏览器
        if visible:
            raise CaptureError(f"浏览器启动失败：{exc}") from exc
        raise CaptureError(
            f"浏览器启动失败：{exc}",
            hint="无头模式可能崩渲染进程，加 --visible 重试；"
                 "或 `python -m playwright install chromium` / 改用系统 Chrome"
                 "（config.capture.browser = chrome）",
        ) from exc


_PAGE_EVAL = """async (arg) => {
  const graph = arg.graph, ms = arg.ms;
  const wait = (t) => new Promise((r) => setTimeout(r, t));
  // `--eval-timeout`：graphToPrompt 是同步重活，卡住时页面不会自己回来。
  // 用 Promise.race 在外层兜一个超时，比等 Playwright 的默认值可控。
  const guard = (p) => Promise.race([
    p,
    new Promise((_, rej) => setTimeout(() => rej(new Error('graphToPrompt 超时（' + ms + 's）')), ms * 1000)),
  ]);
  await app.loadGraphData(graph);
  await wait(__SETTLE__);
  const normal = await guard(app.graphToPrompt());
  // 旁路（mode=4）的节点会被 graphToPrompt 直接丢掉，引用它们的下游输入也一起没了。
  // 想看到"这个流本来能吃几张图"，就得把旁路节点临时打开再抓一份完整图（附录 §7.9）。
  const nodes = app.graph._nodes || app.graph.nodes || [];
  const skipped = [];
  for (const n of nodes) {
    if (n && n.mode === 4) {
      skipped.push(String(n.id));
      n.mode = 0;
    }
  }
  let full = null;
  if (skipped.length) {
    await wait(__SETTLE__);
    full = (await guard(app.graphToPrompt())).output;
  }
  return { output: normal.output, full: full, skipped: skipped };
}"""


def _read_source(page, rel_path: str) -> dict[str, Any]:
    """从实例的 userdata 里读原始 UI 工作流（子目录整段 URL 编码，`/`→`%2F`）。"""
    encoded = urllib.parse.quote("workflows/" + rel_path, safe="")
    js = """async (name) => {
      const r = await fetch('/userdata/' + name);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    }"""
    try:
        return page.evaluate(js, encoded)
    except Exception as exc:  # pragma: no cover - 依赖真实实例
        raise CaptureError(
            f"读不到实例里的工作流文件 {rel_path}：{exc}",
            hint="确认文件在前端的 workflows 目录里、名字没改；或用 --file 导入",
        ) from exc


def _override_capture(
    cfg: Config,
    *,
    nav_timeout: int | None = None,
    eval_timeout: int | None = None,
    browser: str | None = None,
    browser_exe: str | None = None,
) -> Config:
    """拷一份 config，只改 capture 段（配置文件不动）。"""
    import copy

    clone = copy.deepcopy(cfg)
    if nav_timeout is not None:
        clone.capture.nav_timeout = nav_timeout
        clone.capture.page_ready_timeout = nav_timeout
    if eval_timeout is not None:
        clone.capture.eval_timeout = eval_timeout
    if browser:
        clone.capture.browser = browser
    if browser_exe:
        clone.capture.browser_exe = browser_exe
    return clone


def capture(
    cfg: Config,
    targets: list[Target],
    *,
    visible: bool = False,
    strict: bool = True,
    on_result=None,
    tmp_dir: Path | None = None,
    nav_timeout: int | None = None,
    eval_timeout: int | None = None,
    browser: str | None = None,
    browser_exe: str | None = None,
) -> tuple[list[Captured], list[dict[str, str]]]:
    """逐个转换。

    `strict=True` 时第一个失败就抛（单条目转换）；`strict=False` 时把失败收集起来
    继续下一个（`--all` 的失败隔离），返回 `(成功列表, 失败列表)`。

    `nav_timeout` / `eval_timeout` / `browser` / `browser_exe` 只覆盖本次调用，
    **不写回 config**（附录 §7.8：这四个都是命令行上的临时手段）。
    """
    if not targets:
        return [], []
    sync_playwright = _require_playwright()
    ensure_writable_temp(tmp_dir)
    # 覆盖只在本次生效：拷一份 config 换个壳，免得改到磁盘上的 config.json
    if nav_timeout or eval_timeout or browser or browser_exe:
        cfg = _override_capture(
            cfg, nav_timeout=nav_timeout, eval_timeout=eval_timeout,
            browser=browser, browser_exe=browser_exe,
        )
    cap = cfg.capture
    results: list[Captured] = []
    failures: list[dict[str, str]] = []

    with sync_playwright() as p:
        browser = _launch(p, cfg, visible=visible)
        try:
            context = browser.new_context(
                locale=cap.locale,
                viewport={"width": cap.viewport[0], "height": cap.viewport[1]},
            )
            page = context.new_page()
            page.goto(cfg.base_url, timeout=cap.nav_timeout * 1000)
            page.wait_for_function(
                "() => window.app && window.app.graphToPrompt",
                timeout=cap.page_ready_timeout * 1000,
            )
            script = _PAGE_EVAL.replace("__SETTLE__", str(cap.settle_ms))
            for target in targets:
                try:
                    ui = _read_source(page, target.rel_path)
                    # 注意：sync API 的 evaluate 不接受 timeout 参数（传了会报 unexpected keyword）
                    raw = page.evaluate(script, {"graph": ui, "ms": cap.eval_timeout})
                    api = raw.get("output") if isinstance(raw, dict) else None
                    full = raw.get("full")
                    if not isinstance(full, dict) or not full:
                        full = None
                    fallback = False
                    if not isinstance(api, dict) or not api:
                        # 照文件状态导不出来、但"全部启用"能导出来：整个流都被旁路了
                        # （实测 wf-0011：45 个节点里 44 个 mode=4，唯一启用的 MarkdownNote
                        # 没有输出，于是普通图的 output 是空的）。这时拿完整图当 api.json，
                        # 否则这个条目永远转不了。
                        if not full:
                            raise CaptureError("graphToPrompt 没返回 output")
                        api, fallback = full, True
                    skipped = [str(x) for x in (raw.get("skipped") or [])]
                except CwfError as exc:
                    if strict:
                        raise CaptureError(
                            f"{target.entry_id}：{exc.message}", hint=exc.hint
                        ) from exc
                    failures.append({"id": target.entry_id, "error": exc.message})
                    continue
                except Exception as exc:  # pragma: no cover - 依赖真实浏览器
                    if strict:
                        raise CaptureError(
                            f"{target.entry_id} 转换失败：{exc}",
                            hint="用 --visible 看着它跑一遍，或改用 --file 导入",
                        ) from exc
                    failures.append({"id": target.entry_id, "error": str(exc)})
                    continue
                captured = Captured(
                    target=target, api=api, full=full, skipped=skipped, fallback=fallback
                )
                results.append(captured)
                if on_result is not None:
                    on_result(target, api)
        finally:
            try:
                browser.close()
            except Exception:  # pragma: no cover
                pass
    return results, failures


def load_api_file(path: Path) -> dict[str, Any]:
    """导入人工导出的 API 图，做最小结构校验。"""
    if not path.is_file():
        raise CwfError(
            f"文件不存在：{path}",
            code=ExitCode.USAGE,
            error_code="NO_FILE",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise CwfError(
            f"不是合法 JSON：{path}（{exc}）",
            code=ExitCode.USAGE,
            error_code="BAD_API_FILE",
        ) from exc
    return validate_api_graph(raw, where=str(path))


def validate_api_graph(raw: Any, *, where: str) -> dict[str, Any]:
    expected = '{"节点ID": {"class_type": "...", "inputs": {...}}}'
    if not isinstance(raw, dict) or not raw:
        raise CwfError(
            f"{where} 不是 API 格式的工作流（应为 {expected}）",
            code=ExitCode.USAGE,
            error_code="BAD_API_FILE",
            hint="前端菜单：工作流 → 导出（API）",
        )
    for node_id, node in raw.items():
        if not isinstance(node, dict) or "class_type" not in node:
            raise CwfError(
                f"{where} 的节点 {node_id} 没有 class_type，不是 API 格式",
                code=ExitCode.USAGE,
                error_code="BAD_API_FILE",
                hint="别导出 UI 格式（带 nodes/links 的那种），要「导出（API）」",
            )
        if not isinstance(node.get("inputs", {}), dict):
            raise CwfError(
                f"{where} 的节点 {node_id} 的 inputs 不是对象",
                code=ExitCode.USAGE,
                error_code="BAD_API_FILE",
            )
    return raw
