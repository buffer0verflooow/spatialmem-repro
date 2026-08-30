"""Structural anchor relation evaluation (P1-c).

SpatialMem 把门/窗/墙当作记忆树的 L1 结构性锚点层，并在其评测里给出
**每种锚点的关系得分**（Scene 1：门 0.82 / 窗 0.82 / 墙 0.88）。这里的
`evaluate_anchors` 用同样的口径度量：把预测锚点与 GT 锚点按
`type + direction + 距离容差` 匹配，输出每种类型的 grounding F1（即锚点关系
得分），以及宏观 F1 与可选的关系支撑率。
"""

from __future__ import annotations

from dataclasses import dataclass

ANCHOR_TYPES = ("door", "window", "wall")
DIRECTIONS = ("left", "right", "front", "back")

# SpatialMem Scene 1 报告的各锚点关系得分，用于输出时对比。
PAPER_BASELINE = {"door": 0.82, "window": 0.82, "wall": 0.88}


@dataclass(frozen=True)
class Anchor:
    type: str
    name: str = ""
    direction: str = ""
    distance_m: float = 0.0
    confidence: float = 1.0

    @classmethod
    def from_dict(cls, d: dict) -> Anchor:
        return cls(
            type=str(d.get("type", "")).strip(),
            name=str(d.get("name", "")).strip(),
            direction=str(d.get("direction", "")).strip(),
            distance_m=float(d.get("distance_m", 0.0) or 0.0),
            confidence=float(d.get("confidence", 1.0) or 1.0),
        )


def _f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def match_anchors(
    pred: list[Anchor],
    gt: list[Anchor],
    distance_tol_m: float = 2.0,
    require_direction: bool = True,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Greedily match predicted anchors to GT anchors.

    匹配条件：type 一致、direction 一致（require_direction=True 时）、距离差
    ≤ distance_tol_m；距离差最小的优先。返回
    `(pairs, unmatched_pred_idx, unmatched_gt_idx)`。
    """
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for i, p in enumerate(pred):
        best_j: int | None = None
        best_diff: float | None = None
        for j, g in enumerate(gt):
            if j in used:
                continue
            if p.type != g.type:
                continue
            if require_direction and p.direction != g.direction:
                continue
            diff = abs(p.distance_m - g.distance_m)
            if diff > distance_tol_m:
                continue
            if best_diff is None or diff < best_diff:
                best_j, best_diff = j, diff
        if best_j is not None:
            pairs.append((i, best_j))
            used.add(best_j)
    matched_pred = {i for i, _ in pairs}
    unmatched_pred = [i for i in range(len(pred)) if i not in matched_pred]
    unmatched_gt = [j for j in range(len(gt)) if j not in used]
    return pairs, unmatched_pred, unmatched_gt


def evaluate_anchors(
    pred: list[Anchor],
    gt: list[Anchor],
    distance_tol_m: float = 2.0,
    require_direction: bool = True,
) -> dict:
    """Return per-type anchor grounding metrics and the aggregate relation score."""
    pairs, unmatched_pred, unmatched_gt = match_anchors(
        pred, gt, distance_tol_m, require_direction
    )
    per_type: dict[str, dict] = {}
    for t in ANCHOR_TYPES:
        tp = sum(1 for _, j in pairs if gt[j].type == t)
        fp = sum(1 for i in unmatched_pred if pred[i].type == t)
        fn = sum(1 for j in unmatched_gt if gt[j].type == t)
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        per_type[t] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "paper_baseline": PAPER_BASELINE[t],
        }

    tp = len(pairs)
    fp = len(unmatched_pred)
    fn = len(unmatched_gt)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    involved_types = [t for t in ANCHOR_TYPES if per_type[t]["tp"] + per_type[t]["fp"] + per_type[t]["fn"] > 0]
    macro_f1 = (
        sum(per_type[t]["f1"] for t in involved_types) / len(involved_types)
        if involved_types
        else 0.0
    )
    return {
        "precision": precision,
        "recall": recall,
        "anchor_f1": _f1(precision, recall),
        "relation_score": macro_f1,
        "per_type": per_type,
        "matched": len(pairs),
        "extra": fp,
        "missing": fn,
    }


def relation_support(
    pred: list[Anchor],
    gt: list[Anchor],
    gt_relations: list[dict],
    distance_tol_m: float = 2.0,
) -> float:
    """Fraction of GT relations whose both endpoints are grounded in prediction.

    关系 `{subject, predicate, object}` 的 subject/object 需对应 GT 锚点的
    `name`；两端都被预测命中才算该关系被支撑。
    """
    if not gt_relations:
        return 1.0
    pairs, _, _ = match_anchors(pred, gt, distance_tol_m)
    matched_names = {gt[j].name for _, j in pairs}
    supported = 0
    for rel in gt_relations:
        if rel.get("subject") in matched_names and rel.get("object") in matched_names:
            supported += 1
    return supported / len(gt_relations)
