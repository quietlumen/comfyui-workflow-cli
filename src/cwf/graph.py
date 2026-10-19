"""图的最小只读分析：格式识别、节点摘要、指纹、功能分类（附录 §3.1 / §10.1）。

M1 只用得上"不需要连线回溯"的那部分；完整连线解析在 M2 的 inspect 里补齐。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

RULE_VERSION = 5   # 分类规则版本：变了就重算 auto 分类（meta.function_rule）

VIDEO_OUTPUTS = ("SaveAnimatedWEBP", "VHS_VideoCombine", "SaveWEBM", "SaveVideo", "CreateVideo")
UPSCALE_OUTPUTS = ("ImageUpscaleWithModel",)


def is_save_node(class_type: str) -> bool:
    """写盘节点：`SaveImage` / `SaveImageAdvanced` / `SaveVideo` …（各种自定义变体）。"""
    return bool(class_type) and (
        class_type.startswith("Save") or class_type in ("VHS_VideoCombine", "Image Save")
    )


def reachable_from_saves(api: dict[str, Any]) -> set[str]:
    """能从"写盘节点"倒着走到的节点集合；用来把悬空节点从建议里剔掉。

    真实流里常有画布上没连线的备用 LoadImage，它们不算这个流的输入。
    """
    if not isinstance(api, dict):
        return set()
    roots = [
        str(k)
        for k, v in api.items()
        if isinstance(v, dict) and is_save_node(str(v.get("class_type") or ""))
    ]
    if not roots:
        return set(api)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = api.get(node_id)
        if not isinstance(node, dict):
            continue
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] in api and value[0] not in seen:
                    stack.append(value[0])
    return seen



def detect_format(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "unknown"
    if isinstance(raw.get("nodes"), list) and isinstance(raw.get("links"), list):
        return "ui"
    values = [v for v in raw.values() if isinstance(v, dict)]
    if values and all("class_type" in v for v in values):
        return "api"
    return "unknown"


def _ui_nodes(raw: dict) -> list[dict]:
    return [n for n in raw.get("nodes", []) if isinstance(n, dict)]


def node_types(raw: Any) -> list[str]:
    """节点类型列表：UI 取 `nodes[].type`，API 取各值的 `class_type`。"""
    fmt = detect_format(raw)
    if fmt == "ui":
        return [str(n.get("type") or "") for n in _ui_nodes(raw)]
    if fmt == "api":
        return [str(v.get("class_type") or "") for v in raw.values() if isinstance(v, dict)]
    return []


def fingerprint(raw: Any) -> Counter:
    """`{class_type × 次数}` 多重集——判断"还是那个工作流"用的。"""
    return Counter(t for t in node_types(raw) if t)


def jaccard(a: Counter, b: Counter) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 0.0


def similarity(a: Counter, b: Counter) -> float:
    """"还是那个工作流"的相似度：Jaccard，或在体量可比时用包含度。

    只加了一个节点、只删了一个节点都很常见，纯 Jaccard 会误判成"不认识"。
    但包含度对小图套大图太宽松，所以要求体量比 ≥ 0.5 才采信。
    """
    total_a, total_b = sum(a.values()), sum(b.values())
    if not total_a or not total_b:
        return 1.0 if total_a == total_b else 0.0
    inter = sum((a & b).values())
    containment = inter / min(total_a, total_b)
    ratio = min(total_a, total_b) / max(total_a, total_b)
    if ratio >= 0.5:
        return max(jaccard(a, b), containment)
    return jaccard(a, b)


def summarize_nodes(raw: Any) -> dict[str, Any]:
    fmt = detect_format(raw)
    if fmt == "ui":
        nodes = _ui_nodes(raw)
        bypassed = sum(1 for n in nodes if n.get("mode") == 4)
        muted = sum(1 for n in nodes if n.get("mode") == 2)
        total = len(nodes)
    elif fmt == "api":
        total = len([v for v in raw.values() if isinstance(v, dict)])
        bypassed = muted = 0
    else:
        total = bypassed = muted = 0
    return {"total": total, "bypassed": bypassed, "muted": muted, "unknown_types": []}


def classify_function(raw: Any) -> tuple[str, str]:
    """返回 `(function, evidence)`。

    顺序是从真实数据里学出来的：
    - 视频优先：`*Video*` 节点决定一切（SaveVideo / CreateVideo / *ImageToVideo …）；
    - 再认生成意图：`*TextEncode*`（各家自定义文本编码器）+ 采样器/引导器；
    - 纯放大（有放大节点但没有生成链）兜在后面——挂在生成链尾部的放大节点**不该**
      把"文生图"判成 "upscale"（这个坑 `z_image_turbo_文生图` 踩过）。
    """
    types = node_types(raw)
    if not types:
        return "other", "无法解析节点类型"

    has_text = any("TextEncode" in t or t == "CLIPTextEncode" for t in types)
    has_sampler = any("Sampler" in t or "Guider" in t for t in types)
    generates = has_text and has_sampler
    has_image_in = "LoadImage" in types
    upscale_node = next((t for t in types if t in UPSCALE_OUTPUTS or "Upscale" in t), None)

    # 视频输出可能是"生成"出来的，也可能只是"处理"视频帧的（放大/转码）
    video_out = next(
        (
            t
            for t in types
            if t in VIDEO_OUTPUTS or t.endswith("ToVideo") or "VideoCombine" in t
        ),
        None,
    )
    # 有些视频模型的"生成"节点本身就是 xxToVideo（MiniMaxH3ImageToVideo / WanImageToVideo），
    # 它们所在的流可能没有 TextEncode 节点，所以判定生成意图要看这两个来源。
    videos_generated = any(
        t.endswith("ToVideo") or "ImageToVideo" in t or "TextToVideo" in t for t in types
    )
    if video_out is not None:
        if generates or videos_generated:
            return "video", f"有视频生成链 + 输出 {video_out}"
        if upscale_node is not None:
            return "upscale", f"有放大节点 {upscale_node}（视频帧放大，不是生成）"
        return "video", f"有视频输出 {video_out}，无生成链"

    if generates:
        if has_image_in:
            return "image_edit", "有 LoadImage + TextEncode + 采样器（图生图/编辑）"
        return "text_to_image", "有 TextEncode + 采样器，且无图像输入"
    if upscale_node is not None:
        return "upscale", f"有放大节点 {upscale_node}，但无生成链"
    if has_image_in:
        return "image_edit", "有 LoadImage，无生成链"
    return "other", "没命中任何规则"
