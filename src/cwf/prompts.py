"""交互确认：非交互且没给 --yes 时退出码 7（PRD §10）。"""

from __future__ import annotations

import sys

import typer

from . import cost
from .context import Ctx
from .errors import CwfError, ExitCode


def require_yes(ctx: Ctx, question: str, *, details: list[str] | None = None) -> None:
    if ctx.yes:
        return
    if sys.stdin.isatty():  # pragma: no cover - 交互路径
        for line in details or []:
            typer.echo(line)
        if typer.confirm(question, default=False):
            return
        raise CwfError("已取消", code=ExitCode.OK, error_code="CANCELED")
    hint = "非交互环境下加 --yes 才会执行"
    if details:
        hint = "；".join(details) + "；" + hint
    raise CwfError(
        f"需要确认：{question}",
        code=ExitCode.NEED_CONFIRM,
        error_code="NEED_CONFIRM",
        hint=hint,
    )


def gate(ctx: Ctx, entry, payloads: list[dict] | None = None) -> None:
    """PRD §10 的两道运行关口：首次未验证 + 高成本任务。

    `run` / `batch` / `queue run` 共用——三条路径都真的会占显卡，没有谁是"安全的旁路"。
    `payloads` 是**这次真的要发出去**的包（看过模式与上传结果之后再判），不是台账。
    """
    reasons: list[str] = []
    if not entry.meta.get("verified"):
        reasons.append("第一次跑这个转换结果（跑成功一次就记为 verified）")
    seen: set[str] = set()
    for payload in payloads or []:
        for reason in cost.scan(payload):
            if reason not in seen:
                seen.add(reason)
                reasons.append(reason)
    if not reasons:
        return
    require_yes(
        ctx,
        f"确认提交 {entry.id}（{entry.name}）？",
        details=reasons + ["会真的占用显卡"],
    )
    _mark_verified(entry)


def _mark_verified(entry) -> None:
    from .io_utils import atomic_write_json

    meta = dict(entry.meta)
    meta["verified"] = True
    atomic_write_json(entry.dir / "meta.json", meta)
    entry.meta = meta
