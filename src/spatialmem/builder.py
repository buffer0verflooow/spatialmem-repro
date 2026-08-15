"""从 M2 产物（anchors / supports / instances / 度量点云）装配记忆树。

逻辑原在 scripts/build_memory.py，抽出为可复用函数供描述层（M3）与
评测（M4）共用，保证三处装配口径一致。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .memory import SpatialMemory
from .relations import predicate_above_below, predicate_near, predicate_on


def build_memory_from_artifacts(
    *,
    anchors: list[dict],
    supports: Optional[list[dict]],
    instances: list[dict],
    points_metric: np.ndarray,
    near_m: float = 1.0,
    on_z_tol: float = 0.12,
    t_s: float = 0.0,
) -> SpatialMemory:
    """装配：room → anchors/supports → objects + on/above/below/near/in 关系。"""
    pts = np.asarray(points_metric)
    extent_min = pts.min(axis=0)
    extent_max = pts.max(axis=0)

    mem = SpatialMemory()
    room = mem.add_node(
        "room",
        label="室内",
        category="indoor_scene",
        box=(
            float(extent_min[0]),
            float(extent_min[1]),
            0.0,
            float(extent_max[0]),
            float(extent_max[1]),
            float(extent_max[2]),
        ),
        t_s=t_s,
    )

    for a in anchors:
        mem.add_node(
            "anchor",
            label=a["category"],
            category=a["category"],
            box=tuple(a["box"]),
            parent_id=room.node_id,
            confidence=a.get("confidence", 0.9),
            t_s=t_s,
            node_id=a["anchor_id"],
        )
        mem.add_relation(a["anchor_id"], "in", room.node_id, confidence=0.95, t_s=t_s)

    supports = supports or []
    for s in supports:
        mem.add_node(
            "anchor",
            label=s["category"],
            category=s["category"],
            box=tuple(s["box"]),
            parent_id=room.node_id,
            confidence=0.8,
            t_s=t_s,
            node_id=s["support_id"],
        )
        mem.add_relation(s["support_id"], "in", room.node_id, confidence=0.95, t_s=t_s)

    obj_nodes = []
    for inst in instances:
        family = inst.get("family", inst["class"])
        candidates = inst.get("class_candidates", [])
        aliases = [c["class"] for c in candidates]
        if inst["class"] not in aliases:
            aliases.insert(0, inst["class"])
        node = mem.add_node(
            "object",
            label=inst["class"],
            category=family,
            box=tuple(inst["box3d"]),
            attributes={
                "n_observations": inst["n_observations"],
                "median_conf": inst["median_conf"],
                "family": family,
                "class_candidates": candidates,
                "aliases": aliases,
            },
            evidence=[inst["evidence"]] if inst.get("evidence") else [],
            parent_id=room.node_id,
            confidence=inst["median_conf"],
            t_s=t_s,
            node_id=inst["instance_id"],
        )
        obj_nodes.append(node)

    # 物体两两关系：on / above / below / near（全局度量判定）
    for i in range(len(obj_nodes)):
        for j in range(i + 1, len(obj_nodes)):
            a, b = obj_nodes[i], obj_nodes[j]
            if predicate_on(a.box, b.box):
                mem.add_relation(a.node_id, "on", b.node_id, confidence=0.8, t_s=t_s)
            elif predicate_on(b.box, a.box):
                mem.add_relation(b.node_id, "on", a.node_id, confidence=0.8, t_s=t_s)
            rel = predicate_above_below(a.box, b.box)
            if rel == "above":
                mem.add_relation(a.node_id, "above", b.node_id, confidence=0.8, t_s=t_s)
            elif rel == "below":
                mem.add_relation(b.node_id, "above", a.node_id, confidence=0.8, t_s=t_s)
            if predicate_near(a.box, b.box, near_m):
                mem.add_relation(a.node_id, "near", b.node_id, confidence=0.7, t_s=t_s)

    # 物体 - 支撑面 on 关系（几何判定）
    for node in obj_nodes:
        for s in supports:
            if _on_support(node.box, s["box"], s["top_z"], on_z_tol):
                mem.add_relation(
                    node.node_id, "on", s["support_id"], confidence=0.75, t_s=t_s
                )
    return mem


def _on_support(obj_box: tuple, support_box: tuple, top_z: float, z_tol: float) -> bool:
    from .geometry import footprint_overlap

    z_ok = abs(obj_box[2] - top_z) <= z_tol
    return z_ok and footprint_overlap(obj_box, support_box) >= 0.15
