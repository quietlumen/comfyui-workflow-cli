"""`config.json`：模型、读写、路径探测（附录 §2）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .io_utils import atomic_write_json, read_json_or
from .paths import Workspace

NON_RETRYABLE_DEFAULT = [
    "invalid_input",
    "value_not_in_list",
    "required_input_missing",
]


class InstanceCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    dir: str = ""
    host: str = "127.0.0.1"
    port: int = 8188
    python_exe: str | None = None
    workflows_dir: str | None = None
    input_dir: str | None = None
    output_dir: str | None = None
    comfyui_version: str | None = None
    allow_remote: bool = False


class RunCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    default_timeout: int = 900
    wait_poll_interval: float = 1.0
    download_artifacts: bool = True
    artifact_dir: str | None = None
    verify_on_first_run: bool = True


class CaptureCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    browser: str = "chromium"
    browser_exe: str | None = None
    headless: bool = True
    locale: str = "zh-CN"
    viewport: list[int] = Field(default_factory=lambda: [1600, 1000])
    launch_args: list[str] = Field(default_factory=lambda: ["--disable-gpu"])
    nav_timeout: int = 60
    page_ready_timeout: int = 60
    eval_timeout: int = 120
    settle_ms: int = 150


class QueueCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_attempts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [30, 120, 600])
    poll_interval: float = 2.0
    non_retryable_error_types: list[str] = Field(
        default_factory=lambda: list(NON_RETRYABLE_DEFAULT)
    )


class BatchCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    on_error: Literal["continue", "stop"] = "continue"
    concurrency: int = 1
    confirm_over: int = 10
    report_dir: str | None = None


class UiCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    color: bool = True
    table_max_rows: int = 100


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: int = 2
    instance: InstanceCfg = Field(default_factory=InstanceCfg)
    run: RunCfg = Field(default_factory=RunCfg)
    capture: CaptureCfg = Field(default_factory=CaptureCfg)
    queue: QueueCfg = Field(default_factory=QueueCfg)
    batch: BatchCfg = Field(default_factory=BatchCfg)
    ui: UiCfg = Field(default_factory=UiCfg)

    @property
    def base_url(self) -> str:
        return f"http://{self.instance.host}:{self.instance.port}"


def defaults() -> Config:
    return Config()


def load(ws: Workspace) -> Config:
    raw = read_json_or(ws.config_file, None)
    if not isinstance(raw, dict):
        return defaults()
    return Config.model_validate(raw)


def save(ws: Workspace, cfg: Config) -> None:
    atomic_write_json(ws.config_file, cfg.model_dump(mode="json"))


# --------------------------------------------------------------------------- 探测


def _desktop_shared() -> Path | None:
    """ComfyUI-Desktop 的共享目录（两种已知布局都试一遍）。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    base = Path(local)
    for cand in (base / "Comfy-Desktop" / "ComfyUI-Shared", base / "ComfyUI-Shared"):
        if cand.is_dir():
            return cand
    return base / "Comfy-Desktop" / "ComfyUI-Shared"


def probe_workflows_dir(instance_dir: Path) -> Path | None:
    candidates = [
        instance_dir / "user" / "default" / "workflows",
        instance_dir.parent / "ComfyUI-Shared" / "user" / "default" / "workflows",
    ]
    shared = _desktop_shared()
    if shared is not None:
        candidates.append(shared / "user" / "default" / "workflows")
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def probe_io_dir(instance_dir: Path, name: str) -> Path | None:
    candidates = [
        instance_dir / name,
        instance_dir.parent / "ComfyUI-Shared" / name,
    ]
    shared = _desktop_shared()
    if shared is not None:
        candidates.append(shared / name)
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def probe_python_exe(instance_dir: Path) -> str:
    candidates = [
        instance_dir / ".venv" / "Scripts" / "python.exe",
        instance_dir / "python_embeded" / "python.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return sys.executable


def _existing_dir(value: str | None) -> str | None:
    if not value:
        return None
    p = Path(value)
    return str(p) if p.is_dir() else None


def apply_probe(cfg: Config, instance_dir: Path) -> Config:
    """把能探测到的键填上；探测不到的保持原值（增量更新，不清空）。"""
    inst = cfg.instance
    inst.dir = str(instance_dir)
    wf = probe_workflows_dir(instance_dir) or _existing_dir(inst.workflows_dir)
    if wf is not None:
        inst.workflows_dir = str(wf)
    inp = probe_io_dir(instance_dir, "input")
    out = probe_io_dir(instance_dir, "output")
    if inp is not None:
        inst.input_dir = str(inp)
    if out is not None:
        inst.output_dir = str(out)
    inst.python_exe = probe_python_exe(instance_dir)
    return cfg


def count_json_files(root: Path | None) -> int | None:
    """递归数 `.json`（跳过 `.bak` 与临时文件）。离线/目录不存在时返回 None。"""
    if root is None or not root.is_dir():
        return None
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            low = fn.lower()
            if not low.endswith(".json"):
                continue
            if low.endswith(".json.bak") or low.startswith(".") or low.endswith(".tmp"):
                continue
            total += 1
    return total
