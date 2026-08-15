"""M5.2 机会式确认：候选 → 确认节点的升级规则。

设计（计划文档 §4.6）：只积累证据，不急于下结论；结论靠多源一致，
升级靠自然时机。

确认源与权重：
- multi_view：同一候选的多帧 VLM 语义一致（名称族+颜色）≥ min_views → 0.7
- ocr：物体上的印刷文字（如口红品牌名）→ 0.9（文字是强证据）
- interactive：用户提问"这是什么"时模型给出的答案 → 1.0（最强）

升级条件：同一 (名称族, 颜色) 的确认权重和 ≥ threshold（默认 0.7——
任一确认源（多帧一致/OCR/交互）单独即可升级；多源同向累加提高置信度）。
任一源达到阈值即升级；多源同向累加提高置信度。
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Optional

from .candidates import name_family


@dataclass
class Confirmation:
    source: str  # multi_view | ocr | interactive
    name: str
    color: str = ""
    weight: float = 1.0
    detail: str = ""


SOURCE_WEIGHT = {"multi_view": 0.7, "ocr": 0.9, "interactive": 1.0}


@dataclass
class UpgradeDecision:
    upgrade: bool
    name: str = ""
    color: str = ""
    sources: list[str] | None = None
    confidence: float = 0.0
    reason: str = ""


def _group_key(name: str, color: str) -> tuple[str, str]:
    return name_family(name), (color or "").strip()


def decide_upgrade(
    *,
    view_semantics: list[Optional[tuple[str, str]]],
    confirmations: Optional[list[Confirmation]] = None,
    min_views: int = 2,
    threshold: float = 0.7,
) -> UpgradeDecision:
    """综合多帧一致 + 外部确认（OCR/交互），决定候选是否升级为确认节点。

    [view_semantics] 是该候选各观察帧的 VLM (名称, 颜色)。
    """
    weight_by_group: dict[tuple[str, str], float] = collections.defaultdict(float)
    sources_by_group: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    name_by_group: dict[tuple[str, str], str] = {}

    # ---- 多帧一致：同 (名称族, 颜色) 的帧数 ≥ min_views 算一条 multi_view 确认 ----
    view_count: dict[tuple[str, str], int] = collections.Counter()
    sample_name: dict[tuple[str, str], str] = {}
    for sem in view_semantics:
        if sem is None or not sem[0]:
            continue
        key = _group_key(sem[0], sem[1])
        view_count[key] += 1
        sample_name[key] = sem[0]
    for key, count in view_count.items():
        if count >= min_views:
            weight_by_group[key] += SOURCE_WEIGHT["multi_view"]
            sources_by_group[key].append("multi_view")
            name_by_group[key] = sample_name[key]

    # ---- 外部确认（OCR / 交互）----
    for conf in confirmations or []:
        if not conf.name:
            continue
        key = _group_key(conf.name, conf.color)
        weight_by_group[key] += conf.weight
        sources_by_group[key].append(conf.source)
        name_by_group[key] = conf.name

    if not weight_by_group:
        return UpgradeDecision(upgrade=False, reason="no_confirmation")

    # 取权重最高的一组；同权重时优先 interactive > ocr > multi_view
    best_key = max(
        weight_by_group,
        key=lambda k: (
            weight_by_group[k],
            "interactive" in sources_by_group[k],
            "ocr" in sources_by_group[k],
        ),
    )
    weight = weight_by_group[best_key]
    if weight < threshold:
        return UpgradeDecision(
            upgrade=False,
            reason=f"below_threshold({weight:.2f}<{threshold})",
        )
    family, color = best_key
    name = name_by_group[best_key]
    sources = sorted(set(sources_by_group[best_key]))
    confidence = min(1.0, weight / threshold)
    return UpgradeDecision(
        upgrade=True,
        name=name,
        color=color,
        sources=sources,
        confidence=confidence,
        reason=f"weight={weight:.2f}",
    )


__all__ = ["Confirmation", "SOURCE_WEIGHT", "UpgradeDecision", "decide_upgrade"]
