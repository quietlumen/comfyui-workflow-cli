"""节点模式（旁路 / 静音）：把"靠 skip 控制的输入"变成可编程的开关（附录 §7.9）。

为什么需要这一层
----------------
ComfyUI 里一个节点有三种状态：正常（0）、静音（2，永不执行）、旁路（4，跳过但把
同类型输入透传给下游）。**有些工作流用旁路当开关**——比如 `qwen_image_2.1_图像编辑`
把 6 个 LoadImage 里的 5 个旁路掉，用"开几个 LoadImage"来表示"传几张图"。

`app.graphToPrompt()` 会**直接丢掉**旁路节点，连带着把引用它的下游输入也删掉。
所以照文件状态导出的 `api.json` 里，图片输入一个都不剩 —— 命令行工具完全看不到
"这个流能吃图片"。

做法
----
转换时抓两份图：

- `api.json`      ：照文件里保存的旁路状态（= 前端自己导出的结果，一次 run 默认用它）
- `api.full.json` ：把所有 `mode=4` 的节点临时改回 0 之后再抓（含所有被旁路的节点）

参数绑定/序号一律以 `api.full.json` 为准，否则一开一关旁路，语义键里的序号就漂了。
真要启用某个旁路节点时，从完整图出发，把仍然要旁路的节点裁掉。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .errors import ExitCode, CwfError
from .io_utils import read_json_or

FULL_API_NAME = "api.full.json"


# --------------------------------------------------------------------------- 读图


def full_api_file(entry) -> Path:
    return entry.dir / FULL_API_NAME


def load_full(entry) -> dict[str, Any] | None:
    raw = read_json_or(full_api_file(entry), None)
    return raw if isinstance(raw, dict) and raw else None


def load_canonical(entry, api: dict[str, Any] | None = None) -> dict[str, Any]:
    """参数绑定与序号用的"权威图"：有完整图就用完整图，否则退回 api.json。"""
    full = load_full(entry)
    if full:
        return full
    if api is not None:
        return api
    raw = read_json_or(entry.api_file, None)
    return raw if isinstance(raw, dict) else {}


def skipped_ids(meta: dict[str, Any]) -> set[str]:
    nodes = meta.get("nodes") or {}
    return {str(x) for x in (nodes.get("skipped") or [])}


# --------------------------------------------------------------------------- 类型索引


class UiTypes:
    """从 UI 源文件里取"每个输出槽是什么类型"，旁路透传要靠它判断能不能接。

    API 图不带类型信息，只能借源文件；没有源文件（`--file` 导入的条目）时
    退化成"不判断类型，按第一个有连线的输入透传"。
    """

    def __init__(self, ui: dict[str, Any] | None):
        self.nodes: dict[str, dict[str, Any]] = {}
        if isinstance(ui, dict) and isinstance(ui.get("nodes"), list):
            for node in ui["nodes"]:
                if isinstance(node, dict) and "id" in node:
                    self.nodes[str(node["id"])] = node

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def out_type(self, node_id: str, slot: int) -> str | None:
        node = self.nodes.get(str(node_id)) or {}
        outputs = node.get("outputs") or []
        if 0 <= slot < len(outputs) and isinstance(outputs[slot], dict):
            return outputs[slot].get("type")
        return None

    def linked_inputs(self, node_id: str) -> list[tuple[str, str | None]]:
        """有连线（不是 widget 常量）的输入，按声明顺序 —— 透传的候选来源。"""
        node = self.nodes.get(str(node_id)) or {}
        out: list[tuple[str, str | None]] = []
        for item in node.get("inputs") or []:
            if isinstance(item, dict) and item.get("link") is not None:
                out.append((str(item.get("name")), item.get("type")))
        return out


def _types_compatible(want: Any, got: Any) -> bool:
    if not want or not got:
        return True
    if want == "*" or got == "*":
        return True
    # 有些自定义节点用 "IMAGE,MASK" 这种复合声明
    if isinstance(want, str) and isinstance(got, str):
        return want == got or want in got.split(",") or got in want.split(",")
    return True


# --------------------------------------------------------------------------- 裁剪


def _resolve(
    api: dict[str, Any],
    node_id: str,
    slot: int,
    skip: set[str],
    types: UiTypes | None,
    seen: set[tuple[str, int]],
) -> list[Any] | None:
    """把一个引用解析成"真实上游"；沿途的旁路节点按同类型输入透传。"""
    if node_id not in skip:
        return [str(node_id), slot]
    key = (node_id, slot)
    if key in seen:          # 环形引用：放弃透传
        return None
    seen.add(key)

    node = api.get(node_id) or {}
    inputs = node.get("inputs") or {}
    if not isinstance(inputs, dict):
        return None
    wanted = types.out_type(node_id, slot) if types else None
    if types:
        pairs = types.linked_inputs(node_id)
    else:
        pairs = [(str(k), None) for k, v in inputs.items() if _is_link(v)]

    for name, in_type in pairs:
        ref = inputs.get(name)
        if not _is_link(ref):
            continue
        if not _types_compatible(wanted, in_type):
            continue
        got = _resolve(api, str(ref[0]), int(ref[1]), skip, types, seen)
        if got is not None:
            return got
    return None


def _is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


def _id_key(node_id: str):
    return (int(node_id) if node_id.isdigit() else 10**9, node_id)


def prune(api: dict[str, Any], skip: set[str], types: UiTypes | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """从图里裁掉 `skip` 里的节点，返回 `(新图, 变更说明)`。

    ComfyUI 自己的规则是"旁路 = 跳过执行 + 同类型输入透传"，这里照着做：
    下游指向被旁路节点的引用，改接到它第一个同类型的、有连线的输入；接不上就删掉
    这条输入（这正是前端导出时会发生的事）。
    """
    payload = copy.deepcopy(api)
    skip = {str(s) for s in skip if str(s) in payload}
    info: dict[str, Any] = {
        "skipped": sorted(skip, key=_id_key),
        "forwarded": [],
        "dropped": [],
    }
    if not skip:
        return payload, info

    for node_id, node in payload.items():
        if node_id in skip or not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for name in list(inputs.keys()):
            value = inputs[name]
            if not _is_link(value) or str(value[0]) not in skip:
                continue
            got = _resolve(payload, str(value[0]), int(value[1]), skip, types, set())
            if got is None:
                del inputs[name]
                info["dropped"].append({"node": node_id, "input": str(name)})
            else:
                inputs[name] = got
                info["forwarded"].append(
                    {"node": node_id, "input": str(name), "to": f"{got[0]}.{got[1]}"}
                )

    for node_id in skip:
        payload.pop(node_id, None)
    return payload, info


# --------------------------------------------------------------------------- 组包


def build_payload(
    entry,
    *,
    enable: set[str] | None = None,
    skip: set[str] | None = None,
    ui: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按"启用/旁路"两组合成真正要提交的图。

    - 没有要启用的节点 → 直接用 `api.json`（前端导出的原样，风险为零）
    - 要启用旁路节点 → 从 `api.full.json` 出发，把"仍然是旁路"的节点裁掉
    """
    from .errors import usage

    enable = {str(x) for x in (enable or ())}
    skip = {str(x) for x in (skip or ())}
    saved = skipped_ids(entry.meta)
    default_api = read_json_or(entry.api_file, None)
    if not isinstance(default_api, dict) or not default_api:
        raise CwfError(
            f"{entry.id} 还没有 api.json",
            code=ExitCode.USAGE,
            error_code="NO_API",
            hint=f"先 cwf convert {entry.id}",
        )

    turn_on = enable & saved
    missing = enable - saved - set(default_api)
    if missing:
        raise usage(
            f"这些节点不在图里：{', '.join(sorted(missing, key=_id_key))}",
            f"cwf inspect {entry.id} --nodes 看节点 ID",
        )

    info: dict[str, Any] = {
        "enabled": sorted(turn_on, key=_id_key),
        "from": "api.json",
        "skipped": [],
        "forwarded": [],
        "dropped": [],
    }
    if not turn_on:
        payload, sub = prune(default_api, skip, UiTypes(ui))
        info.update({k: sub[k] for k in ("skipped", "forwarded", "dropped")})
        return payload, info

    full = load_full(entry)
    if not full:
        raise usage(
            f"{entry.id} 没有 api.full.json（老版本转换的），启用不了旁路节点",
            f"重新转换一次：cwf convert {entry.id} --force",
        )
    still_off = (saved - turn_on) | skip
    payload, sub = prune(full, still_off, UiTypes(ui))
    info.update({"from": "api.full.json"})
    info.update({k: sub[k] for k in ("skipped", "forwarded", "dropped")})
    return payload, info


def describe(info: dict[str, Any]) -> list[str]:
    """给人看的模式变更说明。"""
    lines: list[str] = []
    if info.get("enabled"):
        lines.append(f"启用旁路节点：{', '.join(info['enabled'])}（完整图 api.full.json）")
    auto_off = [str(x) for x in (info.get("auto_skipped") or [])]
    if auto_off:
        lines.append(f"图片参数没给值 → 旁路：{', '.join(auto_off)}")
    if info.get("forced"):
        lines.append(f"显式保留（--enable）：{', '.join(info['forced'])}")
    left = [str(x) for x in (info.get("skipped") or []) if str(x) not in auto_off]
    if left:
        lines.append(f"保持旁路：{', '.join(left)}")
    if info.get("forwarded"):
        lines.append(f"{len(info['forwarded'])} 条下游连线被透传")
    if info.get("dropped"):
        lines.append(f"{len(info['dropped'])} 条下游连线被删（被旁路节点没有可透传的输入）")
    return lines
