"""ComfyUI HTTP 客户端（附录 §10.4）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from pathlib import Path

from .config import Config
from .errors import CwfError, ExitCode


class ComfyNotComfyUI(Exception):
    """端口有响应，但看起来不是 ComfyUI。"""


@dataclass
class Stats:
    online: bool
    version: str | None = None
    vram_total: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_remote(cfg: Config) -> bool:
    return cfg.instance.host not in LOOPBACK


def guard_remote(cfg: Config, *, allow: bool, command: str) -> None:
    """非本机地址要显式 `--allow-remote`（PRD §7 / N 条：默认不出本机）。

    只拦"会连出去并干活"的命令（`run`/`batch`/`convert`/`upload`/`queue run`）；
    `status`/`list` 这类诊断命令照样能看——否则配错了就没法自证。
    """
    if allow or cfg.instance.allow_remote or not is_remote(cfg):
        return
    raise CwfError(
        f"{cfg.instance.host} 不是本机地址，{command} 需要 --allow-remote",
        code=ExitCode.USAGE,
        error_code="REMOTE_NOT_ALLOWED",
        hint="真要把工作流发到远端，加 --allow-remote；或 cwf init --host 127.0.0.1",
    )


def _client(cfg: Config, timeout: float) -> httpx.Client:
    """本地实例**不走**系统代理。

    踩过的坑：机器上配了 `HTTP_PROXY` 时，httpx 会把手快 `127.0.0.1:8188` 的请求
    也发给代理，结果是 502 空响应——看起来像 ComfyUI 挂了，其实是代理插了一手。
    """
    trust_env = is_remote(cfg)
    return httpx.Client(base_url=cfg.base_url, timeout=timeout, trust_env=trust_env)


def fetch_stats(cfg: Config, timeout: float = 3.0) -> Stats:
    """连不上 → online=False（不抛异常，离线是正常状态）。"""
    base = cfg.base_url
    try:
        with _client(cfg, timeout) as http:
            resp = http.get("/system_stats")
            if resp.status_code != 200:
                raise ComfyNotComfyUI(f"HTTP {resp.status_code}")
            payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return Stats(online=False)

    system = payload.get("system") if isinstance(payload, dict) else None
    if not isinstance(system, dict):
        raise ComfyNotComfyUI(f"{base} 不是 ComfyUI")

    vram = None
    devices = payload.get("devices") or []
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        vram = devices[0].get("vram_total")

    return Stats(
        online=True,
        version=system.get("comfyui_version"),
        vram_total=vram if isinstance(vram, int) else None,
        raw=payload,
    )


def fetch_queue_depth(cfg: Config, timeout: float = 3.0) -> tuple[int | None, int | None]:
    """返回 (running, pending)；失败一律 (None, None)。"""
    try:
        with _client(cfg, timeout) as http:
            resp = http.get("/queue")
            if resp.status_code != 200:
                return None, None
            payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    running = payload.get("queue_running")
    pending = payload.get("queue_pending")
    return (
        len(running) if isinstance(running, list) else None,
        len(pending) if isinstance(pending, list) else None,
    )


def post_prompt(cfg: Config, payload: dict[str, Any], *, client_id: str) -> str:
    """提交给 `/prompt`，返回 prompt_id。失败时原样带出 node_errors。"""
    body = {"prompt": payload, "client_id": client_id}
    try:
        with _client(cfg, 30.0) as http:
            resp = http.post("/prompt", json=body)
    except httpx.HTTPError as exc:
        raise CwfError(
            f"提交失败：{exc}",
            code=ExitCode.OFFLINE,
            error_code="OFFLINE",
            hint="确认 ComfyUI 还开着：cwf status",
        ) from exc

    if resp.status_code in (200, 201):
        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise CwfError("ComfyUI 没返回 prompt_id", error_code="BAD_RESPONSE")
        return str(prompt_id)

    detail = "未知错误"
    node_errors: dict[str, Any] = {}
    try:
        data = resp.json()
        detail = str(data.get("error") or data)
        node_errors = data.get("node_errors") or {}
    except ValueError:
        detail = resp.text[:400]
    raise CwfError(
        f"ComfyUI 拒绝了这次提交：{detail}",
        code=ExitCode.EXEC_FAILED,
        error_code="EXEC_FAILED",
        node_errors=node_errors,
        hint="参数/模型名不对时先看 node_errors；也可以用 cwf inspect 看输入",
    )


def get_history(cfg: Config, prompt_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """`/history/<prompt_id>`；还没跑完时返回 None。"""
    try:
        with _client(cfg, timeout) as http:
            resp = http.get(f"/history/{prompt_id}")
            if resp.status_code != 200:
                return None
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CwfError(
            f"查询执行结果失败：{exc}",
            code=ExitCode.OFFLINE,
            error_code="OFFLINE",
        ) from exc
    if not isinstance(data, dict) or not data:
        return None
    entry = data.get(prompt_id)
    if entry is None:
        first = next(iter(data.values()), None)
        return first if isinstance(first, dict) else None
    return entry if isinstance(entry, dict) else None


def download_output(cfg: Config, item: dict[str, Any], *, timeout: float = 60.0) -> bytes:
    params = {
        "filename": item.get("filename"),
        "subfolder": item.get("subfolder") or "",
        "type": item.get("type") or "output",
    }
    try:
        with _client(cfg, timeout) as http:
            resp = http.get("/view", params=params)
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPError as exc:
        raise CwfError(
            f"下载产物失败：{item.get('filename')}（{exc}）",
            code=ExitCode.ERROR,
            error_code="DOWNLOAD_FAILED",
        ) from exc

def upload_image(cfg: Config, path: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """上传一张图到实例的 input 目录，返回可直接当参数用的名字。"""
    if not path.is_file():
        raise CwfError(f"文件不存在：{path}", code=ExitCode.USAGE, error_code="NO_FILE")
    try:
        with _client(cfg, 120.0) as http:
            with path.open("rb") as fh:
                resp = http.post(
                    "/upload/image",
                    files={"image": (path.name, fh, "application/octet-stream")},
                    data={"overwrite": "true" if overwrite else "false"},
                )
    except httpx.HTTPError as exc:
        raise CwfError(
            f"上传失败：{exc}",
            code=ExitCode.OFFLINE,
            error_code="OFFLINE",
            hint="cwf status 看实例是否在线",
        ) from exc
    if resp.status_code not in (200, 201):
        raise CwfError(
            f"ComfyUI 拒绝了上传（HTTP {resp.status_code}）：{resp.text[:200]}",
            code=ExitCode.EXEC_FAILED,
            error_code="UPLOAD_FAILED",
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("name"):
        raise CwfError("上传成功但没拿到文件名", error_code="UPLOAD_FAILED")
    return data

def fetch_object_info(cfg: Config, timeout: float = 30.0) -> dict[str, Any]:
    """`/object_info` 全量节点定义（类型、combo 选项、范围）。"""
    try:
        with _client(cfg, timeout) as http:
            resp = http.get("/object_info")
            if resp.status_code != 200:
                raise CwfError(
                    f"/object_info 返回 HTTP {resp.status_code}",
                    code=ExitCode.OFFLINE,
                    error_code="OFFLINE",
                )
            data = resp.json()
    except httpx.HTTPError as exc:
        raise CwfError(
            f"拉 /object_info 失败：{exc}",
            code=ExitCode.OFFLINE,
            error_code="OFFLINE",
            hint="实例离线时只能用值推断类型；连上之后再跑一次可自动校正",
        ) from exc
    if not isinstance(data, dict):
        raise CwfError("/object_info 不是对象", error_code="BAD_RESPONSE")
    return data