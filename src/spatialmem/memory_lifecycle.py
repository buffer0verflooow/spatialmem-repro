"""M5.4 遗忘与纠正：错误标签纠正、物体移动归档、陈旧节点标记。

设计（计划文档 §4.6）：
- 纠正：用户说「不是电风扇，是暖风机」→ 更新标签并降置信度，记录纠正日志，
  避免错误标签永久污染；
- 移动：物体被挪走 → 旧位置带时间戳归档（保留「之前在那」的可答性），
  更新当前位置；
- 陈旧：长时间未再见的节点标记 stale，由上层决定归档/遗忘。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CorrectionLog:
    node_id: str
    old_name: str
    new_name: str
    old_color: str
    new_color: str
    source: str
    t_s: float


@dataclass
class ArchivedPosition:
    node_id: str
    old_center: list[float]
    archived_at_s: float


def apply_correction(
    node: dict,
    *,
    name: Optional[str] = None,
    color: Optional[str] = None,
    source: str = "interactive",
    t_s: float = 0.0,
    confidence_factor: float = 0.5,
) -> tuple[dict, CorrectionLog]:
    """用户纠正 → 更新标签/颜色，置信度降档，记录日志。"""
    old_name = node.get("name", "")
    old_color = node.get("color", "")
    new = dict(node)
    if name:
        new["name"] = name
    if color:
        new["color"] = color
    new["confidence"] = round(node.get("confidence", 1.0) * confidence_factor, 3)
    new["sources"] = list(node.get("sources", [])) + [source]
    log = CorrectionLog(
        node_id=node.get("node_id") or node.get("candidate_id", ""),
        old_name=old_name,
        new_name=new.get("name", ""),
        old_color=old_color,
        new_color=new.get("color", ""),
        source=source,
        t_s=t_s,
    )
    return new, log


def move_node(
    node: dict,
    new_center: list[float],
    *,
    t_s: float = 0.0,
) -> tuple[dict, Optional[ArchivedPosition]]:
    """物体移动 → 旧位置带时间戳归档，更新中心。"""
    old_center = node.get("center")
    if old_center is None:
        new = dict(node)
        new["center"] = list(new_center)
        return new, None
    new = dict(node)
    new["center"] = list(new_center)
    archived = ArchivedPosition(
        node_id=node.get("node_id") or node.get("candidate_id", ""),
        old_center=[float(v) for v in old_center],
        archived_at_s=t_s,
    )
    return new, archived


def stale_status(
    nodes: list[dict],
    *,
    now_s: float,
    max_age_s: float,
) -> dict[str, str]:
    """按最近出现时间把节点标记 active/stale。"""
    out: dict[str, str] = {}
    for node in nodes:
        nid = node.get("node_id") or node.get("candidate_id", "")
        last = node.get("last_seen_s") or node.get("last_seen") or 0.0
        if isinstance(last, str):
            last = 0.0  # 字符串帧名无法转秒，按未记录处理（不判陈旧）
        out[nid] = "stale" if (now_s - float(last)) > max_age_s else "active"
    return out


__all__ = ["CorrectionLog", "ArchivedPosition", "apply_correction", "move_node", "stale_status"]
