"""工作区布局：所有路径只在这里推算（附录 §1）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ENV_HOME = "CWF_HOME"


def _default_workspace() -> Path:
    """默认工作区：`D:\\comfyui_workflow\\data`；盘符不存在时退到 `~/.cwf`。"""
    preferred = Path("D:/comfyui_workflow/data")
    try:
        if preferred.drive and Path(preferred.drive + "/").exists():
            return preferred
    except OSError:  # pragma: no cover - 极端环境
        pass
    return Path.home() / ".cwf"


def resolve_workspace(cli_value: str | None = None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env = os.environ.get(_ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    return _default_workspace()


@dataclass(frozen=True)
class Workspace:
    root: Path

    # ---- 顶层文件 ----
    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def object_info_cache(self) -> Path:
        return self.cache_dir / "object_info.json"

    @property
    def rules_dir(self) -> Path:
        return self.root / "rules"

    @property
    def rules_file(self) -> Path:
        return self.rules_dir / "params.json"

    @property
    def workflows_dir(self) -> Path:
        return self.root / "workflows"

    @property
    def ids_file(self) -> Path:
        return self.workflows_dir / "_ids.json"

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def queue_jobs(self) -> Path:
        return self.queue_dir / "jobs.jsonl"

    @property
    def queue_state(self) -> Path:
        return self.queue_dir / "state.json"

    @property
    def batch_dir(self) -> Path:
        return self.root / "batch"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "cwf.log"

    # ---- 每个工作流一个目录 ----
    def entry_dir(self, entry_id: str) -> Path:
        return self.workflows_dir / entry_id

    def ensure_layout(self) -> None:
        """只建顶层目录；条目的子目录由创建它的命令自己建。"""
        for d in (self.root, self.cache_dir, self.rules_dir, self.workflows_dir,
                  self.queue_dir, self.batch_dir, self.outputs_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def initialized(self) -> bool:
        return self.config_file.is_file()
