"""进程存活检测：Windows 走 ctypes，POSIX 走 os.kill（附录 §10.6）。"""

from __future__ import annotations

import os
import sys

_IS_WIN = sys.platform == "win32"


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if not _IS_WIN:
        try:
            os.kill(pid, 0)          # POSIX：只检查，不发送信号
        except ProcessLookupError:
            return False
        except PermissionError:
            return True              # 存在但不属于当前用户
        return True

    # Windows：**绝不**用 os.kill(pid, 0)——它等价于 TerminateProcess
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def current_pid() -> int:
    return os.getpid()
