"""高成本判定（PRD §10.2）：视频节点、`batch_size ≥ 10`、`megapixels ≥ 4`。

只看**这次要提交的 payload**，不看台账——同一个流用文生图参数跑可能很便宜，
换成 8 秒视频就很贵，判据必须跟着参数走。
"""

from __future__ import annotations

from typing import Any

from .graph import VIDEO_OUTPUTS

VIDEO_HINTS = ("VideoCombine", "ToVideo", "SaveVideo", "SaveWEBM", "SaveAnimatedWEBP")
BATCH_OVER = 10
MEGAPIXELS_OVER = 4.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def scan(payload: dict[str, Any]) -> list[str]:
    """返回"为什么贵"的人话清单；空列表 = 不拦。"""
    if not isinstance(payload, dict):
        return []
    video: list[str] = []
    heavy_batch: list[str] = []
    heavy_mp: list[str] = []
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if class_type in VIDEO_OUTPUTS or any(h in class_type for h in VIDEO_HINTS):
            video.append(f"{node_id}:{class_type}")
        inputs = node.get("inputs") or {}
        batch_size = _num(inputs.get("batch_size"))
        if batch_size is not None and batch_size >= BATCH_OVER:
            heavy_batch.append(f"{node_id} batch_size={int(batch_size)}")
        megapixels = _num(inputs.get("megapixels"))
        if megapixels is not None and megapixels >= MEGAPIXELS_OVER:
            heavy_mp.append(f"{node_id} megapixels={megapixels:g}")

    reasons: list[str] = []
    if video:
        reasons.append(f"有视频节点（{'、'.join(video[:3])}）")
    if heavy_batch:
        reasons.append(f"批量张数 ≥ {BATCH_OVER}（{'、'.join(heavy_batch[:3])}）")
    if heavy_mp:
        reasons.append(f"像素量 ≥ {MEGAPIXELS_OVER:g}MP（{'、'.join(heavy_mp[:3])}）")
    return reasons
