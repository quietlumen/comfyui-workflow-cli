"""`cwf upload`（PRD §7 FR-3）：把本地图片放进实例的 input，拿到可当参数用的名字。"""

from __future__ import annotations

from pathlib import Path

import typer

from ..client import guard_remote, upload_image
from ..config import load
from ..context import Ctx
from ..errors import not_initialized
from ..output import payload


def run(ctx: Ctx, path: str, *, overwrite: bool, allow_remote: bool = False) -> None:
    ws = ctx.ws
    if not ws.initialized:
        raise not_initialized(str(ws.root))
    cfg = load(ws)
    guard_remote(cfg, allow=allow_remote, command="upload")
    data = upload_image(cfg, Path(path), overwrite=overwrite)
    name = data.get("name")
    sub = data.get("subfolder") or ""
    usable = f"{sub}/{name}" if sub else name

    body = payload(uploaded=usable, raw=data)

    def human() -> None:
        ctx.out.line(f"[green]✓[/green] 已上传：{usable}")
        ctx.out.line(f"  现在可以把它当参数用：cwf run <ID> --param image={usable}")

    ctx.out.emit(body, human)


def register(app: typer.Typer) -> None:
    @app.command("upload")
    def upload_cmd(
        ctx: typer.Context,
        path: str = typer.Argument(..., help="本地图片路径"),
        overwrite: bool = typer.Option(False, "--overwrite", help="同名就覆盖"),
        allow_remote: bool = typer.Option(
            False, "--allow-remote", help="允许传往非本机实例"
        ),
    ) -> None:
        """把图片传到实例的 input 目录（返回可用作参数的名字）。"""
        run(ctx.obj, path, overwrite=overwrite, allow_remote=allow_remote)