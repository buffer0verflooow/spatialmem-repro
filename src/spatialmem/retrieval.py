"""M5.3 检索优先问答：确认记忆 → 先查后答，画面兜底。

流程：用户问「X 在哪 / 什么颜色」→
  1. 记忆优先：在确认节点（M5.2 产物）里按名称/颜色检索，命中即答，
     并标注「根据记忆」+（若有 3D 锚点与位姿）方向/距离；
  2. 画面兜底：记忆未命中才走当前画面分析（离线验证中只标记 fallback）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .candidates import name_family
from .query import expand_query
from .relations import egocentric_direction


@dataclass
class MemoryNode:
    node_id: str
    name: str
    color: str = ""
    source: str = "multi_view"  # multi_view | ocr | interactive
    confidence: float = 1.0
    center: Optional[list] = None  # 3D 锚点（可能有）
    label_hint: str = ""


@dataclass
class Answer:
    found: bool
    text: str
    matches: list[dict] = field(default_factory=list)
    fallback_used: bool = False


class ConfirmedMemory:
    """确认节点检索层。"""

    def __init__(self, nodes: list[MemoryNode]) -> None:
        self._nodes = nodes

    @classmethod
    def from_json(cls, confirmed: list[dict]) -> "ConfirmedMemory":
        return cls(
            [
                MemoryNode(
                    node_id=c.get("candidate_id", ""),
                    name=c.get("name", ""),
                    color=c.get("color", ""),
                    source=c.get("sources", ["multi_view"])[0]
                    if c.get("sources") else "multi_view",
                    confidence=c.get("confidence", 1.0),
                    center=c.get("center"),
                    label_hint=c.get("label_hint", ""),
                )
                for c in confirmed
            ]
        )

    def nodes(self) -> list[MemoryNode]:
        return self._nodes

    def query(
        self,
        text: str,
        *,
        viewer_pose: Optional[np.ndarray] = None,
    ) -> Answer:
        """名称/颜色检索；命中优先输出「根据记忆」回答。"""
        q = (text or "").strip()
        if not q:
            return Answer(found=False, text="", fallback_used=True)
        q_clean = _clean_query(q)
        keywords = expand_query(q_clean)
        hits: list[tuple[MemoryNode, int]] = []
        for node in self._nodes:
            score = self._score(node, q_clean, keywords)
            if score > 0:
                hits.append((node, score))
        hits.sort(key=lambda t: (-t[1], -t[0].confidence))

        # 名称命中优先；纯颜色查询（如「白色」「有哪些白色」）才允许颜色匹配，
        # 避免「不存在的蓝色恐龙」误答成蓝色物体。
        name_hits = [h for h in hits if h[1] >= 2]
        color_hits = [h for h in hits if h[1] == 1]
        if name_hits:
            hits = name_hits
        elif color_hits and _is_color_query(q_clean):
            hits = color_hits
        else:
            hits = []

        if not hits:
            return Answer(
                found=False,
                text="记忆中没找到，让我看看画面",
                fallback_used=True,
            )

        lines: list[str] = []
        details: list[dict] = []
        for node, score in hits[:3]:
            where = self._where(node, viewer_pose)
            prefix = f"{node.color}的{node.name}" if node.color else node.name
            details.append(
                {
                    "node_id": node.node_id,
                    "name": node.name,
                    "color": node.color,
                    "score": score,
                    "source": node.source,
                    "confidence": node.confidence,
                    "where": where,
                }
            )
            lines.append(f"{prefix}：{where}")
        text = "根据记忆，" + "；".join(lines)
        return Answer(found=True, text=text, matches=details, fallback_used=False)

    def _score(self, node: MemoryNode, q: str, keywords: list[str]) -> int:
        name = (node.name or "").strip()
        color = (node.color or "").strip()
        hint = (node.label_hint or "").strip()
        if name_family(name) == name_family(q) or (name and (name in q or q in name)):
            return 3
        if hint and (hint == q or q in hint):
            return 2
        if any(k and (k in name or name in k) for k in keywords):
            return 2
        if color and (color in q or q in color):
            return 1
        return 0

    def _where(self, node: MemoryNode, viewer_pose: Optional[np.ndarray]) -> str:
        if node.center is not None and viewer_pose is not None:
            info = egocentric_direction(np.asarray(node.center, dtype=float), viewer_pose)
            tag = {
                "front": "前方", "left": "左方", "right": "右方", "behind": "后方",
                "left_front": "左前方", "right_front": "右前方",
            }.get(info["tag"], info["tag"])
            return f"在{tag}约 {info['distance']:.1f} 米（{node.source}确认，置信 {node.confidence:.0%}）"
        if node.center is not None:
            return f"位置坐标 {[round(v, 2) for v in node.center]}（{node.source}确认）"
        return f"在场景中见过（{node.source}确认，置信 {node.confidence:.0%}）"


__all__ = ["MemoryNode", "Answer", "ConfirmedMemory"]


def _is_color_query(q: str) -> bool:
    """「白色」「红色」等纯颜色词（≤3 字且以 色 结尾）。"""
    q = q.strip()
    return len(q) <= 3 and q.endswith("色")


def _clean_query(q: str) -> str:
    """去掉口语疑问词：「风扇在哪」→「风扇」，「白色电风扇什么颜色」→「白色电风扇」。"""
    for w in (
        "在哪里", "在哪儿", "在哪", "哪里", "哪儿",
        "什么颜色", "颜色", "是什么东西", "是什么", "是啥", "在什么地方",
    ):
        q = q.replace(w, "")
    return q.strip()
