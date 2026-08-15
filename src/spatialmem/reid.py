"""M5.4 视觉再识别：同一物体跨帧/跨视角/跨会话认出来。

两级匹配：
- 强匹配：VLM 语义一致（名称族相同 + 颜色一致）→ 同一物体，高置信；
- 弱匹配：外观相似度 ≥ 阈值（实测 0.95，见 run_m5_reid 验证）且颜色不矛盾
  → 同一物体，中置信（解决「马桶/电风扇」这类语义抖动）。

外观特征在灰调背景下区分度有限（不同物体也可能 0.9+），因此弱匹配阈值取
0.95 并加颜色矛盾护栏；真正鲁棒的身份要靠视觉嵌入（列为后续升级，M5.4 用
轻量特征 + 语义组合先打通机制）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .candidates import appearance_distance, name_family, same_color


@dataclass
class ReidEntry:
    node_id: str
    feature: Optional[np.ndarray] = None
    semantic: Optional[tuple[str, str]] = None  # (name, color)


@dataclass
class ReidResult:
    match_id: Optional[str]
    tier: str  # strong | weak | none
    similarity: float = 0.0
    confidence: float = 0.0


def reidentify(
    query_feature: np.ndarray,
    query_semantic: Optional[tuple[str, str]],
    entries: list[ReidEntry],
    *,
    sim_threshold: float = 0.95,
) -> ReidResult:
    """在已知记忆里找与查询最像的节点。

    - 强匹配：语义名称族相同且颜色一致 → 直接命中；
    - 弱匹配：外观相似 ≥ [sim_threshold] 且颜色不矛盾；
    - 都不满足 → 新物体（返回 none）。
    """
    best_weak: tuple[Optional[str], float] = (None, 0.0)
    for e in entries:
        if (
            query_semantic is not None
            and e.semantic is not None
            and same_color(query_semantic[1], e.semantic[1])
            and name_family(query_semantic[0]) == name_family(e.semantic[0])
        ):
            return ReidResult(
                match_id=e.node_id,
                tier="strong",
                similarity=1.0,
                confidence=1.0,
            )
        if e.feature is None or query_feature is None:
            continue
        sim = 1.0 - appearance_distance(query_feature, e.feature)
        # 颜色矛盾护栏：语义都在且颜色不同 → 弱匹配不成立
        if (
            query_semantic is not None
            and e.semantic is not None
            and not same_color(query_semantic[1], e.semantic[1])
        ):
            continue
        if sim > best_weak[1]:
            best_weak = (e.node_id, sim)
    if best_weak[0] is not None and best_weak[1] >= sim_threshold:
        return ReidResult(
            match_id=best_weak[0],
            tier="weak",
            similarity=best_weak[1],
            confidence=min(1.0, best_weak[1]),
        )
    return ReidResult(match_id=None, tier="none", similarity=best_weak[1])


__all__ = ["ReidEntry", "ReidResult", "reidentify"]
