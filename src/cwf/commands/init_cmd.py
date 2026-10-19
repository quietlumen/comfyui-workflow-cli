"""`cwf init`（PRD §7 FR-1）。"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from .. import browser
from ..context import Ctx
from ..output import payload
from ..client import ComfyNotComfyUI, fetch_stats
from ..config import Config, apply_probe, count_json_files, defaults, load, save
from ..errors import usage
from ..paths import Workspace


def _nearest_dirs(bad: Path) -> list[Path]:
    """在同级目录里找名字含 comfy 的，帮忙猜路径。"""
    guesses: list[Path] = []
    for base in (bad.parent, bad.parent.parent):
        try:
            for child in sorted(base.iterdir()):
                if child.is_dir() and "comfy" in child.name.lower():
                    guesses.append(child)
        except OSError:
            continue
    return guesses[:5]


def _resolve_instance_dir(raw: str | None, cfg: Config) -> Path:
    value = raw or cfg.instance.dir or None
    if value is None:
        if sys.stdin.isatty():  # pragma: no cover - 交互路径
            value = typer.prompt("ComfyUI 安装目录")
        else:
            raise usage(
                "缺少 --instance-dir，且工作区里还没有记录过实例目录",
                "例：cwf init --instance-dir \"D:\\ComfyUI-Installs\\ComfyUI\\ComfyUI\"",
            )
    path = Path(value).expanduser()
    if not path.is_dir():
        guesses = _nearest_dirs(path)
        hint = "目录不存在"
        if guesses:
            hint += "；近似候选：" + "，".join(str(g) for g in guesses)
        raise usage(f"实例目录不存在：{path}", hint)
    return path.resolve()


def run(
    ctx: Ctx,
    *,
    instance_dir: str | None,
    host: str | None,
    port: int | None,
    python_exe: str | None,
    reset: bool,
    no_probe: bool,
) -> None:
    ws: Workspace = ctx.ws
    existed = ws.initialized
    ws.ensure_layout()

    cfg: Config = defaults() if reset else load(ws)
    if instance_dir:
        cfg.instance.dir = instance_dir
    target = _resolve_instance_dir(instance_dir, cfg)

    warnings: list[str] = []
    # 本地路径探测永远做（离线安装也要能定位工作流目录）；--no-probe 只跳过联网与浏览器预检
    cfg = apply_probe(cfg, target)
    if host:
        cfg.instance.host = host
    if port:
        cfg.instance.port = port
    if python_exe:
        cfg.instance.python_exe = python_exe

    online = False
    version: str | None = None
    port_busy_wrong = False
    if not no_probe:
        try:
            stats = fetch_stats(cfg)
            online = stats.online
            version = stats.version
            if online:
                cfg.instance.comfyui_version = version
        except ComfyNotComfyUI as exc:
            port_busy_wrong = True
            warnings.append(f"{cfg.base_url} 有响应但不是 ComfyUI（{exc}）")
        except Exception as exc:  # pragma: no cover - 兜底
            warnings.append(f"在线探测失败：{exc}")

    if not no_probe and not online and not port_busy_wrong:
        warnings.append("实例当前离线；不影响登记，`cwf status` 会照常显示本地信息")

    save(ws, cfg)

    workflows_dir = cfg.instance.workflows_dir
    found = count_json_files(Path(workflows_dir)) if workflows_dir else None
    if workflows_dir is None:
        warnings.append("没找到实例的工作流目录，`cwf sync` 将无从扫描")
    elif found == 0:
        warnings.append(f"工作流目录是空的：{workflows_dir}")

    ready = None if no_probe else browser.browser_ready()
    if ready is False:
        warnings.append("缺 Playwright 浏览器：python -m playwright install chromium")

    data = payload(
        workspace=str(ws.root),
        config=str(ws.config_file),
        instance_dir=cfg.instance.dir,
        online=online,
        comfyui_version=version,
        workflows_dir=workflows_dir,
        workflows_found=found,
        python_exe=cfg.instance.python_exe,
        browser_ready=ready,
        created=not existed or reset,
        warnings=warnings,
    )

    def human() -> None:
        out = ctx.out
        out.heading("初始化完成" if not existed or reset else "配置已更新")
        out.kv("工作区", str(ws.root), ok=True)
        out.kv("配置文件", str(ws.config_file))
        out.kv("实例目录", cfg.instance.dir)
        out.kv(
            "实例状态",
            f"在线（ComfyUI {version or '版本未知'}）" if online else "离线",
            ok=online,
        )
        if workflows_dir:
            out.kv("工作流目录", f"{workflows_dir}（{found if found is not None else '?'} 个 .json）")
        else:
            out.kv("工作流目录", "未找到", ok=False)
        if cfg.instance.python_exe:
            out.kv("实例 Python", cfg.instance.python_exe)
        out.kv("浏览器", browser.describe(ready), ok=bool(ready) if ready is not None else None)
        for w in warnings:
            out.line(f"  [yellow]![/yellow] {w}")
        if not ctx.out.quiet:
            out.line("")
            out.line("下一步：cwf status 看全貌，cwf sync 把工作流收进台账。")

    ctx.out.emit(data, human)


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init_cmd(
        ctx: typer.Context,
        instance_dir: str | None = typer.Option(None, "--instance-dir", "-d", help="ComfyUI 安装目录"),
        host: str | None = typer.Option(None, "--host", help="实例主机（默认 127.0.0.1）"),
        port: int | None = typer.Option(None, "--port", help="实例端口（默认 8188）"),
        python_exe: str | None = typer.Option(None, "--python-exe", help="覆盖自动探测的 Python"),
        reset: bool = typer.Option(False, "--reset", help="整份覆盖 config（默认增量合并）"),
        no_probe: bool = typer.Option(False, "--no-probe", help="跳过在线探测与浏览器预检"),
    ) -> None:
        """初始化工作区与实例配置（离线也能成功）。"""
        run(
            ctx.obj,
            instance_dir=instance_dir,
            host=host,
            port=port,
            python_exe=python_exe,
            reset=reset,
            no_probe=no_probe,
        )
