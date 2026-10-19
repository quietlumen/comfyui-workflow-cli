"""退出码与统一错误对象（附录 §8）。"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1        # 一般错误 / 部分失败
    USAGE = 2        # 用法、参数、依赖缺失、ID 多解
    OFFLINE = 3
    EXEC_FAILED = 5
    NEED_CONFIRM = 7


class CwfError(Exception):
    """所有可预期的失败都抛它，由 cli.main 统一转成退出码 + JSON。"""

    def __init__(
        self,
        message: str,
        *,
        code: ExitCode = ExitCode.ERROR,
        error_code: str = "ERROR",
        hint: str | None = None,
        node_errors: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.error_code = error_code
        self.hint = hint
        self.node_errors = node_errors or {}
        self.extra = extra or {}

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "error_code": self.error_code,
            "node_errors": self.node_errors,
            "hint": self.hint,
        }
        payload.update(self.extra)
        return payload


def usage(message: str, hint: str | None = None) -> CwfError:
    return CwfError(message, code=ExitCode.USAGE, error_code="USAGE", hint=hint)


def not_initialized(ws_root: str) -> CwfError:
    return CwfError(
        f"工作区未初始化：找不到 {ws_root} 里的 config.json",
        code=ExitCode.USAGE,
        error_code="NOT_INITIALIZED",
        hint="先跑 cwf init --instance-dir <ComfyUI 目录>",
    )
