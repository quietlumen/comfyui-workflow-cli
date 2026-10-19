"""参数绑定：语义键定位 + CRUD（附录 §4）。

M2 阶段先把"按语义键重新定位"和条目读写立起来；`param add/set/rm` 的 CLI 随后接上。
"""

from __future__ import annotations

from typing import Any

from .io_utils import atomic_write_json
from .paths import Workspace

TYPE_MAP = {
    "INT": "integer",
    "FLOAT": "number",
    "STRING": "string",
    "BOOLEAN": "boolean",
    "COMBO": "combo",
}


def _api_nodes(api: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """按节点 ID 数值排序（API 图没有坐标，ID 就是稳定顺序）。"""
    items = [(str(k), v) for k, v in api.items() if isinstance(v, dict)]
    return sorted(items, key=lambda kv: (int(kv[0]) if kv[0].isdigit() else 10**9, kv[0]))


def is_link(value: Any) -> bool:
    """`[节点id, 输出序号]` 两元组 = 连线；其余 = 常量。"""
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


def semantic_ordinals(api: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    """`(class_type, input) → [节点ID…]`，按下标就是语义键里的 ordinal。"""
    table: dict[tuple[str, str], list[str]] = {}
    for node_id, node in _api_nodes(api):
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for name in inputs:
            table.setdefault((class_type, str(name)), []).append(node_id)
    return table


def make_key(class_type: str, input_name: str, ordinal: int) -> str:
    return f"{class_type}#{input_name}#{ordinal}"


def split_key(key: str) -> tuple[str, str, int]:
    class_type, input_name, ordinal = key.rsplit("#", 2)
    return class_type, input_name, int(ordinal)


def candidates(api: dict[str, Any]) -> list[dict[str, Any]]:
    """候选输入 = 值是常量的输入（**不落库**，`inspect` 每次现算）。"""
    out: list[dict[str, Any]] = []
    table = semantic_ordinals(api)
    for node_id, node in _api_nodes(api):
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for name, value in inputs.items():
            if is_link(value):
                continue
            ordinal = table[(class_type, str(name))].index(node_id)
            out.append(
                {
                    "key": make_key(class_type, str(name), ordinal),
                    "node": node_id,
                    "input": str(name),
                    "class_type": class_type,
                    "value": value,
                }
            )
    return out


def _locate(
    params: list[dict[str, Any]], api: dict[str, Any], *, apply: bool
) -> tuple[list[dict[str, str]], bool]:
    """按语义键重新定位。`apply=False` 时一个字都不改，只报告会改什么。

    唯一一份定位算法，`relocate()`（写）与 `inspect`（只读）共用它。
    """
    table = semantic_ordinals(api)
    drift: list[dict[str, str]] = []
    changed = False
    for param in params:
        key = str(param.get("key") or "")
        if not key:
            continue
        try:
            class_type, input_name, ordinal = split_key(key)
        except (ValueError, IndexError):
            drift.append({"name": str(param.get("name")), "from": str(param.get("node")), "to": "?"})
            if apply and not param.get("orphan"):
                param["orphan"] = True
                changed = True
            continue
        nodes = table.get((class_type, input_name), [])
        if ordinal < len(nodes):
            new_node = nodes[ordinal]
            if param.get("node") != new_node:
                drift.append(
                    {"name": str(param.get("name")), "from": str(param.get("node")), "to": new_node}
                )
                if apply:
                    param["node"] = new_node
                    changed = True
            if apply and param.get("orphan"):
                param["orphan"] = False
                changed = True
        else:
            drift.append({"name": str(param.get("name")), "from": str(param.get("node")), "to": "?"})
            if apply and not param.get("orphan"):
                param["orphan"] = True
                changed = True
    return drift, changed


def plan_relocate(
    params: list[dict[str, Any]], api: dict[str, Any]
) -> list[dict[str, str]]:
    """算出"重新定位会改哪些"——**只算不写**，`inspect` 这种只读命令用它。

    与 `relocate()` 的唯一区别是不写 `params.json`：`inspect` 不允许有副作用，
    但用户需要知道"节点 ID 已经飘了"。真正的落盘仍然只发生在 `convert`/`sync`。
    """
    return _locate(params, api, apply=False)[0]


def relocate(ws: Workspace, entry_dir, params: list[dict[str, Any]], api: dict[str, Any]) -> list[dict[str, str]]:
    """按语义键重新定位节点 ID；定位不到就标 orphan（保留绑定）。"""
    drift, changed = _locate(params, api, apply=True)
    if changed:
        atomic_write_json(entry_dir / "params.json", params)
    return drift


# --------------------------------------------------------------------------- 推荐规则

TECHNICAL_INPUTS = {
    "sampler_name", "scheduler", "filename_prefix", "device", "weight_dtype",
    "type", "output_type", "multiple", "mode_type", "seam_fix_mode",
    "force_uniform_tiles", "tiled_decode", "compare_view", "sampling",
    "clip_name", "vae_name", "unet_name", "ckpt_name", "model_name",
}

SEED_INPUTS = {"seed", "noise_seed"}

_NAME_HINTS = {
    "seed": ("seed", "种子", "basic"),
    "steps": ("steps", "步数", "advanced"),
    "cfg": ("cfg", "CFG", "advanced"),
    "denoise": ("denoise", "重绘幅度", "advanced"),
    "batch_size": ("batch_size", "批量张数", "expert"),
    "width": ("width", "宽", "advanced"),
    "height": ("height", "高", "advanced"),
    "megapixels": ("megapixels", "百万像素", "advanced"),
    "aspect_ratio": ("aspect_ratio", "宽高比", "basic"),
    "upscale_by": ("upscale_by", "放大倍数", "advanced"),
}


def load_user_rules(path) -> list[dict[str, Any]]:
    """`rules/params.json`：一个规则数组，命中的候选优先采用。"""
    from .io_utils import read_json_or

    raw = read_json_or(path, [])
    if isinstance(raw, dict):
        raw = raw.get("rules", [])
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def match_user_rule(rules: list[dict[str, Any]], class_type: str, input_name: str):
    for rule in rules:
        want_class = rule.get("class_type")
        want_input = rule.get("input")
        if want_class is not None and str(want_class) != class_type:
            continue
        if want_input is not None and str(want_input) != input_name:
            continue
        return rule
    return None


def _constant_chain(api: dict[str, Any], node_id: str, input_name: str, depth: int = 6):
    """顺着连线往上找那个真正的常量输入（Primitive 之类的）。

    真实工作流里，提示词到采样器之间常常夹着 PreviewAny / ComfySwitchNode：
    `TextEncodeQwenImage21.prompt ← PreviewAny.source ← ComfySwitchNode.on_false ← Primitive.value`。
    所以这里按"命名输入 → 条件分支 → 任意上游"的顺序走，而不是只认 `text`。
    """
    seen: set[tuple[str, str]] = set()
    current = (str(node_id), str(input_name))
    for _ in range(depth):
        if current in seen:
            return None
        seen.add(current)
        node = api.get(current[0])
        if not isinstance(node, dict):
            return None
        inputs = node.get("inputs") or {}
        if current[1] not in inputs:
            return None
        value = inputs[current[1]]
        if not is_link(value):
            return current

        upstream_id = str(value[0])
        upstream = api.get(upstream_id)
        if not isinstance(upstream, dict):
            return None
        up_inputs = upstream.get("inputs") or {}

        # 1) 条件节点（ComfySwitchNode 之类）：按 switch 当前值走对应分支
        switch = up_inputs.get("switch")
        if isinstance(switch, bool):
            branch = "on_true" if switch else "on_false"
            if branch in up_inputs:
                current = (upstream_id, branch)
                continue

        # 2) 命名输入里的常量（最常见：Primitive 的 value）
        for name in (current[1], "text", "value", "prompt", "string"):
            if name in up_inputs and not is_link(up_inputs[name]):
                return (upstream_id, name)

        # 3) 还没到底：继续往上跟一条连线；实在没有就取第一个常量
        for name, candidate in up_inputs.items():
            if is_link(candidate):
                current = (upstream_id, str(name))
                break
        else:
            for name, candidate in up_inputs.items():
                if not is_link(candidate):
                    return (upstream_id, str(name))
            return None
    return None


def _graph_hints(api: dict[str, Any]) -> dict[str, tuple[str, str, str, str]]:
    """图结构推断：sampler.positive / negative 反追到那个常量输入。"""
    hints: dict[str, tuple[str, str, str, str]] = {}
    for node_id, node in _api_nodes(api):
        if not isinstance(node.get("inputs"), dict):
            continue
        for slot, (name, label), names in (
            ("positive", ("positive_prompt", "正向提示词"),
             ("prompt", "text", "value", "string")),
            ("negative", ("negative_prompt", "负向提示词"),
             ("negative_prompt", "text", "value", "prompt")),
        ):
            value = node["inputs"].get(slot)
            if not is_link(value):
                continue
            # 不同自定义节点的输入名不一样（prompt / text / value…），挨个试
            target = None
            for cand_name in names:
                target = _constant_chain(api, str(value[0]), cand_name)
                if target is not None:
                    break
            if target is None:
                continue
            class_type = str((api.get(target[0]) or {}).get("class_type") or "")
            ordinal = None
            table = semantic_ordinals(api)
            nodes = table.get((class_type, target[1]), [])
            if target[0] in nodes:
                ordinal = nodes.index(target[0])
            if ordinal is None:
                continue
            key = make_key(class_type, target[1], ordinal)
            if key in hints:
                continue
            hints[key] = (
                name,
                label,
                "basic",
                f"sampler.{slot} 反追到 {class_type}.{target[1]}",
            )
    return hints


def _builtin_hint(class_type: str, input_name: str):
    if class_type == "LoadImage" and input_name == "image":
        return ("image", "输入图片", "basic"), ""
    if input_name in TECHNICAL_INPUTS:
        return None, "纯技术参数，默认不推荐"
    if "Loader" in class_type or input_name.endswith("_name"):
        return None, "模型加载器，默认不推荐"
    if input_name in SEED_INPUTS:
        return _NAME_HINTS["seed"], ""
    hint = _NAME_HINTS.get(input_name)
    if hint is not None:
        return hint, ""
    return None, "没命中规则"


def _image_name(used: set[str], title: Any) -> str:
    """图片输入的默认名：有干净的节点标题就用 `image_<标题>`（如 1 / 2 / 3）。"""
    text = str(title or "").strip()
    if text and len(text) <= 8 and not any(ch.isspace() for ch in text):
        cleaned = "".join(ch for ch in text if ch.isalnum() or ch in "-_")
        if cleaned and not cleaned.lower().startswith("load"):
            return f"image_{cleaned}"
    return "image"


def suggest(
    api: dict[str, Any],
    params: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    *,
    skipped: set[str] | None = None,
) -> dict[str, Any]:
    """把候选输入变成建议（四级优先：用户规则 → 内置表 → 图结构 → 启发式）。"""
    from .graph import reachable_from_saves

    skipped = {str(x) for x in (skipped or ())}
    reachable = reachable_from_saves(api)
    bound = {str(p.get("key")) for p in params}
    graph_hints = _graph_hints(api)
    used: set[str] = {str(p.get("name")) for p in params}
    bound_names = {str(p.get("key")): str(p.get("name")) for p in params}
    rows: list[dict[str, Any]] = []

    for cand in candidates(api):
        key = cand["key"]
        class_type = cand["class_type"]
        input_name = cand["input"]
        recommended = True
        source = "builtin"
        reason = ""
        name = label = category = None
        node_id = str(cand["node"])
        node = api.get(node_id) or {}
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = (meta or {}).get("title")

        rule = match_user_rule(rules, class_type, input_name)
        if rule is not None:
            source = "user-rule"
            name = rule.get("name") or input_name
            label = rule.get("label") or name
            category = rule.get("category") or "basic"
            reason = "命中 data/rules/params.json"
            if rule.get("recommended") is False:
                recommended = False
                name = label = category = None
        else:
            hint, why = _builtin_hint(class_type, input_name)
            if hint is not None:
                source = "builtin"
                name, label, category = hint
                if name == "image":
                    name = _image_name(used, title)
                reason = why or f"内置语义表：{input_name}"
            elif why.startswith("没命中规则"):
                inferred = graph_hints.get(key)
                if inferred is not None:
                    source = "inferred"
                    name, label, category, reason = inferred
                else:
                    recommended = False
                    source = "heuristic"
                    reason = "没命中规则，默认不推荐"
            else:
                recommended = False
                source = "builtin"
                reason = why

        if recommended and reachable and node_id not in reachable:
            recommended = False
            source = "heuristic"
            reason = "没连到输出节点（悬空）"

        existing = bound_names.get(key)
        if existing:
            name = existing                      # 已绑定的沿用你自己的名字，不再改名
        if recommended and name and not existing:
            base = name
            if base in used:
                base = f"{base}_{_short_tag(class_type)}"
            name = base
            suffix = 2
            while name in used:
                name = f"{base}_{suffix}"
                suffix += 1
            used.add(name)

        rows.append(
            {
                "key": key,
                "node": node_id,
                "input": input_name,
                "class_type": class_type,
                "title": title,
                "type": _infer_type(cand["value"]),
                "default": cand["value"],
                "options": None,
                "min": None,
                "max": None,
                "skipped": node_id in skipped,
                "reachable": (node_id in reachable) if reachable else True,
                "recommended": recommended,
                "suggested_name": name if recommended else None,
                "label": label if recommended else None,
                "category": category if recommended else None,
                "suggest_source": source if recommended else None,
                "already_bound": key in bound,
                "reason": reason,
                "_node": node_id,
            }
        )

    by_node: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row["recommended"]:
            continue
        bucket = by_node.setdefault(
            row["node"], {"id": row["node"], "class_type": row["class_type"], "params": []}
        )
        bucket["params"].append(row["suggested_name"])
    for row in rows:
        row.pop("_node", None)
    return {"params": rows, "nodes": list(by_node.values())}


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"
def _short_tag(class_type: str) -> str:
    """同名字冲突时给个短标签：UltimateSDUpscale → upscale。"""
    if "Upscale" in class_type:
        return "upscale"
    if "Loader" in class_type:
        return "loader"
    tag = "".join(ch for ch in class_type.lower() if ch.isalnum())
    return tag[:10] or "x"

# --------------------------------------------------------------------------- 运行时合并

def resolve_param(params: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """按 name 或 alias 找一个已绑定参数。"""
    for p in params:
        if p.get("name") == token:
            return p
    low = token.lower()
    for p in params:
        if any(str(a).lower() == low for a in (p.get("aliases") or [])):
            return p
    return None


def available_names(params: list[dict[str, Any]]) -> str:
    return ", ".join(str(p.get("name")) for p in params) or "（还没有参数）"


def parse_kv(pairs: list[str], flag: str = "--param") -> dict[str, str]:
    """`--param k=v`（可重复）→ dict。`run` 与 `queue add` 共用同一份语义。"""
    from .errors import usage

    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise usage(f"{flag} 要写成 k=v，收到 {raw!r}")
        key, value = raw.split("=", 1)
        out[key.strip()] = value
    return out


def merge_values(
    params: list[dict[str, Any]],
    presets: dict[str, Any],
    preset_names: list[str],
    cli_values: dict[str, str],
    *,
    random_seed: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """`default → preset → cli` 逐层覆盖，值按参数类型做强制转换。"""
    from .errors import usage

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for p in params:
        name = str(p.get("name"))
        values[name] = p.get("default")
        sources[name] = "default"

    for preset_name in preset_names:
        preset = presets.get(preset_name)
        if not isinstance(preset, dict):
            raise usage(
                f"没有这个预设：{preset_name}",
                "预设存在工作流的 presets.json 里；用 --save-preset 存一个",
            )
        for token, raw in (preset.get("params") or {}).items():
            p = resolve_param(params, str(token))
            if p is None:
                raise usage(
                    f"预设 {preset_name} 里的 {token} 不是已绑定的参数",
                    f"可用参数：{available_names(params)}",
                )
            values[str(p["name"])] = coerce(p, raw)
            sources[str(p["name"])] = f"preset:{preset_name}"

    for token, raw in cli_values.items():
        p = resolve_param(params, token)
        if p is None:
            raise usage(
                f"--param {token} 不是已绑定的参数",
                f"可用参数：{available_names(params)}；想直接改图上的输入用 --set",
            )
        values[str(p["name"])] = coerce(p, raw)
        sources[str(p["name"])] = "cli"

    if random_seed:
        import random

        for p in params:
            name = str(p.get("name"))
            if str(p.get("input")) in SEED_INPUTS or "seed" in name.lower():
                if p.get("type") in ("integer", "number"):
                    values[name] = random.randint(1, 2**53)
                    sources[name] = "random-seed"
    return values, sources


def coerce(param: dict[str, Any], raw: Any) -> Any:
    """按参数条目声明的类型转换命令行字符串。"""
    from .errors import usage

    type_ = str(param.get("type") or "string")
    name = param.get("name")
    if type_ in ("image", "file"):
        return _coerce_file(param, raw, usage)
    if type_ == "string":
        return raw if isinstance(raw, str) else str(raw)
    if type_ == "integer":
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise usage(f"--param {name} 需要整数，收到 {raw!r}") from None
        return _check_range(param, value, usage)
    if type_ == "number":
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise usage(f"--param {name} 需要数字，收到 {raw!r}") from None
        return _check_range(param, value, usage)
    if type_ == "boolean":
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off"):
            return False
        raise usage(f"--param {name} 需要 true/false，收到 {raw!r}")
    options = param.get("options")
    if type_ == "combo" and isinstance(options, list) and options:
        if raw in options:
            return raw
        hits = [o for o in options if isinstance(o, str) and o.startswith(str(raw))]
        if len(hits) == 1:
            return hits[0]
        raise usage(f"--param {name} 不在可选值里：{raw!r}", f"可选：{', '.join(map(str, options[:10]))}")
    return raw


def _coerce_file(param: dict[str, Any], raw: Any, usage) -> str:
    """图片/文件类输入：本地路径原样留着（由 executor 上传），否则当"已上传的文件名"。"""
    from pathlib import Path

    text = (raw if isinstance(raw, str) else str(raw)).strip()
    if not text:
        return ""
    if Path(text).is_file():
        return text
    options = param.get("options")
    if isinstance(options, list) and options and text not in options:
        hits = [o for o in options if isinstance(o, str) and o.startswith(text)]
        if len(hits) == 1:
            return hits[0]
    return text


def local_file(value: Any) -> str | None:
    """值是本地文件路径就返回它，否则 None（用来决定要不要自动上传）。"""
    from pathlib import Path

    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return str(path) if path.is_file() else None


def apply_values(
    api: dict[str, Any],
    params: list[dict[str, Any]],
    values: dict[str, Any],
    *,
    canonical: dict[str, Any] | None = None,
) -> list[str]:
    """把值写进 payload 的副本；返回无法定位的孤儿参数名。

    `canonical` 是"权威图"（有 `api.full.json` 就用它）：语义键里的序号必须按权威图
    算，否则一启用/旁路节点，序号就整体漂移。节点被旁路掉（不在 payload 里）不算
    孤儿——那是用户故意的。
    """
    table = semantic_ordinals(canonical if canonical is not None else api)
    orphans: list[str] = []
    for p in params:
        name = str(p.get("name"))
        if name not in values:
            continue
        try:
            class_type, input_name, ordinal = split_key(str(p.get("key")))
        except (ValueError, IndexError):
            orphans.append(name)
            continue
        nodes = table.get((class_type, input_name), [])
        if ordinal >= len(nodes):
            orphans.append(name)
            continue
        node = api.get(nodes[ordinal])
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
            node["inputs"][input_name] = values[name]
    return orphans


def apply_overrides(api: dict[str, Any], overrides: list[str]) -> list[dict[str, Any]]:
    """`--set NODE.INPUT=V`：一次性改图上某个输入，不落库。"""
    from .errors import usage

    applied: list[dict[str, Any]] = []
    for raw in overrides:
        if "=" not in raw or "." not in raw.split("=", 1)[0]:
            raise usage(f"--set 要写成 NODE.INPUT=VALUE，收到 {raw!r}")
        left, value = raw.split("=", 1)
        node_id, input_name = left.rsplit(".", 1)
        node = api.get(str(node_id))
        if not isinstance(node, dict):
            raise usage(f"--set 的节点 {node_id} 不在图里", "cwf inspect <ID> --nodes 看节点 ID")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or input_name not in inputs:
            raise usage(f"节点 {node_id} 没有输入 {input_name}")
        old = inputs[input_name]
        new: Any = value
        if isinstance(old, bool):
            new = str(value).lower() in ("1", "true", "yes", "on")
        elif isinstance(old, int):
            try:
                new = int(value)
            except ValueError:
                new = value
        elif isinstance(old, float):
            try:
                new = float(value)
            except ValueError:
                new = value
        inputs[input_name] = new
        applied.append({"node": str(node_id), "input": input_name, "value": new})
    return applied

# --------------------------------------------------------------------------- object_info

TYPE_BY_SPEC = {
    "INT": "integer",
    "FLOAT": "number",
    "STRING": "string",
    "BOOLEAN": "boolean",
    "COMBO": "combo",
    "IMAGE": "file",          # 图像/文件类输入按字符串处理
}


def input_spec(object_info: dict[str, Any], class_type: str, input_name: str) -> dict[str, Any] | None:
    """从 `/object_info` 里取某个节点某个输入的声明（类型/选项/范围）。"""
    node = object_info.get(class_type)
    if not isinstance(node, dict):
        return None
    sections = node.get("input") or {}
    for key in ("required", "optional"):
        spec = (sections.get(key) or {}).get(input_name)
        if spec is None:
            continue
        head = spec[0] if isinstance(spec, list) and spec else spec
        meta = spec[1] if isinstance(spec, list) and len(spec) > 1 else {}
        meta = meta if isinstance(meta, dict) else {}
        if isinstance(head, list):
            return {
                "type": "combo",
                "options": list(head),
                "default": meta.get("default"),
                "min": None,
                "max": None,
            }
        type_name = TYPE_BY_SPEC.get(str(head).upper())
        options = meta.get("options")
        if isinstance(options, list) and options and type_name is None:
            return {"type": "combo", "options": options, "default": meta.get("default"),
                    "min": None, "max": None}
        return {
            "type": type_name,
            "options": list(options) if isinstance(options, list) else None,
            "default": meta.get("default"),
            "min": meta.get("min"),
            "max": meta.get("max"),
        }
    return None

def _check_range(param: dict[str, Any], value: float, usage) -> float:
    lo, hi, name = param.get("min"), param.get("max"), param.get("name")
    if isinstance(lo, (int, float)) and value < lo:
        raise usage(f"--param {name} 不能小于 {lo}，收到 {value}")
    if isinstance(hi, (int, float)) and value > hi:
        raise usage(f"--param {name} 不能大于 {hi}，收到 {value}")
    return value


def _raw_declared(specs: dict[str, Any], class_type: str, input_name: str) -> str | None:
    """`/object_info` 里那个输入的**原始**类型名（`INT` / `FLOAT` / `COMBO`…）。"""
    node = specs.get(class_type)
    if not isinstance(node, dict):
        return None
    for section in ("required", "optional"):
        spec = ((node.get("input") or {}).get(section) or {}).get(input_name)
        if spec is None:
            continue
        head = spec[0] if isinstance(spec, list) and spec else spec
        if isinstance(head, list):
            return "COMBO"
        return str(head) if head else None
    return None


def type_drift(
    params: list[dict[str, Any]], specs: dict[str, Any]
) -> list[dict[str, str]]:
    """已绑参数的 `type` 与 `/object_info` 声明不一致的那些。

    为什么要查这件事：`param add` 时如果节点类型还没进缓存（离线、或自定义节点没加载），
    类型只能从当前值猜——`megapixels` 声明是 `FLOAT`、值却是整数 `2`，就会被记成
    `integer`，之后 `--param megapixels=0.5` 直接被 `coerce` 拒掉。缓存在手却不去比对，
    这个错就永远不自我纠正。

    只读本地缓存（`objects.load`），**不刷新、不连实例**，所以 `list`/`show` 可以放心调。
    `specs` 为空（没缓存）时返回空列表——没依据就不猜。
    """
    out: list[dict[str, str]] = []
    if not specs:
        return out
    for param in params:
        key = str(param.get("key") or "")
        try:
            class_type, input_name, _ordinal = split_key(key)
        except (ValueError, IndexError):
            continue
        spec = input_spec(specs, class_type, input_name)
        declared = (spec or {}).get("type")
        stored = param.get("type")
        if not declared or not stored or declared == stored:
            continue
        out.append(
            {
                "name": str(param.get("name")),
                "stored": str(stored),
                "declared": str(declared),
                "raw": str(_raw_declared(specs, class_type, input_name) or declared),
                "key": key,
            }
        )
    return out