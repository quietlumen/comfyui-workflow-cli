"""`/object_info` 缓存：绑定时用它校正参数类型、combo 选项与取值范围。

这是 PRD §7 里"type/default/options/min/max 绑定时从 api.json + object_info 自动带出"
的落地——**从值猜类型会猜错**（`megapixels` 声明是 FLOAT，值却是整数 2）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .client import fetch_object_info
from .config import Config
from .errors import CwfError
from .io_utils import atomic_write_json, read_json_or
from .parameters import input_spec
from .paths import Workspace

MAX_AGE_HOURS = 24


def load(ws: Workspace) -> dict[str, Any]:
    raw = read_json_or(ws.object_info_cache, None)
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        return raw["data"]
    return {}


def _stale(ws: Workspace) -> bool:
    raw = read_json_or(ws.object_info_cache, None)
    if not isinstance(raw, dict):
        return True
    at = raw.get("at")
    if not isinstance(at, str):
        return True
    try:
        when = datetime.strptime(at, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return True
    return (datetime.now() - when).total_seconds() > MAX_AGE_HOURS * 3600


def refresh(ws: Workspace, cfg: Config) -> dict[str, Any]:
    data = fetch_object_info(cfg)
    atomic_write_json(
        ws.object_info_cache,
        {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "data": data},
    )
    return data


def lookup(ws: Workspace, cfg: Config, class_type: str, input_name: str) -> dict[str, Any] | None:
    """取输入声明；缓存里没有这个节点类型就刷新一次。离线时返回 None（退回值推断）。"""
    data = load(ws)
    if class_type not in data and (data == {} or _stale(ws)):
        try:
            data = refresh(ws, cfg)
        except CwfError:
            return None
    spec = input_spec(data, class_type, input_name)
    if spec is None and class_type not in data:
        try:
            data = refresh(ws, cfg)
            spec = input_spec(data, class_type, input_name)
        except CwfError:
            return None
    return spec
