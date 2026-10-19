"""命令上下文：工作区 + 输出器（避免 cli ↔ commands 循环导入）。"""

from __future__ import annotations

from dataclasses import dataclass

from .output import Output
from .paths import Workspace


@dataclass
class Ctx:
    ws: Workspace
    out: Output
    yes: bool = False
    debug: bool = False
