"""`<ID>` 解析：id → alias → name → 唯一前缀（附录 §7.0）。"""

from __future__ import annotations

from .errors import CwfError, ExitCode
from .ledger import Entry


def _ambiguous(token: str, hits: list[Entry]) -> CwfError:
    lines = "\n".join(f"  {e.id}  {e.name}" for e in hits[:10])
    return CwfError(
        f'<ID> "{token}" 匹配到 {len(hits)} 个工作流：\n{lines}',
        code=ExitCode.USAGE,
        error_code="AMBIGUOUS_ID",
        hint=f"用完整 ID（如 cwf run {hits[0].id}），或给常用的那个加别名"
        f"（cwf set {hits[0].id} --alias {token}）",
        extra={"candidates": [{"id": e.id, "name": e.name} for e in hits]},
    )


def _pick(token: str, hits: list[Entry]) -> Entry:
    if not hits:
        raise CwfError(
            f"找不到工作流：{token}",
            code=ExitCode.USAGE,
            error_code="NOT_FOUND",
            hint="cwf list 看有哪些；新文件先 cwf sync",
        )
    if len(hits) > 1:
        raise _ambiguous(token, hits)
    return hits[0]


def resolve(entries: list[Entry], token: str) -> Entry:
    """四段式解析，取第一个**唯一**命中。"""
    token = token.strip()
    if not token:
        raise CwfError("缺少 <ID>", code=ExitCode.USAGE, error_code="USAGE")

    exact = [e for e in entries if e.id == token]
    if exact:
        return exact[0]

    low = token.lower()
    aliases = [
        e for e in entries
        if any(str(a).lower() == low for a in (e.meta.get("aliases") or []))
    ]
    if aliases:
        return _pick(token, aliases)

    names = [e for e in entries if e.name.lower() == low]
    if names:
        return _pick(token, names)

    prefix = [e for e in entries if e.id.lower().startswith(low)]
    if prefix:
        return _pick(token, prefix)

    raise CwfError(
        f"找不到工作流：{token}",
        code=ExitCode.USAGE,
        error_code="NOT_FOUND",
        hint="cwf list 看有哪些；新文件先 cwf sync",
    )
