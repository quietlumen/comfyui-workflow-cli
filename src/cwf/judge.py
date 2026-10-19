# -*- coding: utf-8 -*-
"""新增：结果"正确性"自检 + 日志落盘方案（CWFSKILL 层，不动内核逻辑）。

背景：真实 ComfyUI 对"不存在的模型 / 空 latent / 被旁路的 SaveImage"也会回
`status_str: success`，但 outputs 为空——此时 cwf 的 run 也会记 success + 0 产物，
agent 无法判断这次调用到底对不对。

判断结果是否正确（agent 用）：
  1) run --json 直接给 status / artifacts / error / node_errors；
  2) 有产物 + 文件存在 + 尺寸合理 = 成功；
  3) success 但 artifacts==0 = "疑似空跑"：要么图被旁路（有意的），要么节点没执行；
  4) 看 history 明细：status_str==success 且 execution_cached 全部 nodes 时=完全缓存；
  5) 记录自带 submitted_prompt（这次真正发出去的包），可复核参数是否生效。
"""

from __future__ import annotations


def judge_run_result(data: dict) -> dict:
    """把一次 run/log 的 JSON 判成一句话结论（给 agent 汇报用）。"""
    status = data.get("status")
    artifacts = data.get("artifacts") or []
    error = data.get("error")
    node_errors = data.get("node_errors") or {}
    run_id = data.get("run_id")
    if status == "failed":
        detail = "；".join(
            f"{nid}: {e}" for nid, e in (node_errors or {}).items()
        ) or error
        return {"verdict": "失败", "reason": detail, "run_id": run_id}
    if status == "timeout":
        return {"verdict": "超时", "reason": error, "run_id": run_id}
    if status == "success":
        local = [a for a in artifacts if a.get("local_path")]
        if not artifacts:
            return {
                "verdict": "疑似空跑",
                "reason": "status=success 但 0 产物：检查 SaveImage 是否被旁路/节点是否执行",
                "run_id": run_id,
            }
        return {"verdict": "成功", "artifacts": [a.get("local_path") for a in local or artifacts],
                "run_id": run_id}
    return {"verdict": "未知", "reason": f"status={status}", "run_id": run_id}


def check_artifact(file_path: str) -> dict:
    """产物落地核对：存在、非空、可读（图像可读尺寸）。"""
    from pathlib import Path
    import struct

    p = Path(file_path)
    out = {"exists": False}
    if not p.is_file():
        out["error"] = "文件不存在"
        return out
    size = p.stat().st_size
    out.update({"exists": True, "size": size})
    if size == 0:
        out["error"] = "空文件（0 字节）"
        return out
    head = p.read_bytes()[:64]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", head[16:24])
        out.update({"kind": "png", "width": w, "height": h})
    elif head[:3] == b"\xff\xd8\xff":
        out.update({"kind": "jpeg"})
    elif head[4:8] == b"ftyp":
        out.update({"kind": "mp4/webm-like"})
    else:
        out.update({"kind": "other"})
    return out
