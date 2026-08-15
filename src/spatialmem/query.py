"""Query engine: locate nodes, walk relation chains, produce egocentric answers."""

from __future__ import annotations

from typing import Optional

import numpy as np

from .memory import Node, SpatialMemory
from .relations import egocentric_direction


SYNONYMS = {
    "水桶": ["bucket", "plastic bucket", "water bucket"],
    "桶": ["bucket", "trash can", "trash bin", "wastebin"],
    "垃圾桶": ["trash can", "trash bin", "wastebin", "waste basket", "garbage can"],
    "花筒": ["vase", "flower vase", "bucket", "flower pot"],
    "花瓶": ["vase", "flower vase", "bottle"],
    "杯子": ["cup", "mug", "bottle"],
    "遥控器": ["remote", "remote control"],
    "鼠标": ["mouse", "computer mouse"],
    "键盘": ["keyboard"],
    "手机": ["cell phone", "phone", "smartphone"],
    "被子": ["blanket", "quilt"],
    "枕头": ["pillow", "cushion"],
    "床": ["bed"],
    "桌子": ["table", "desk", "coffee table"],
    "沙发": ["sofa", "couch"],
    "椅子": ["chair", "armchair", "office chair"],
    "风扇": ["fan", "electric fan", "standing fan"],
    "电视": ["tv", "television"],
    "笔记本电脑": ["laptop"],
    "瑜伽垫": ["trash can", "trash bin", "bucket"],  # until cloud VLM renames it
}


def expand_query(text: str) -> list[str]:
    """Return query keywords: the raw text plus synonyms for its tokens."""
    raw = text.strip().lower()
    keys = [raw] if raw else []
    # longest-first so "水桶"/"垃圾桶" cover the generic "桶" instead of
    # bleeding trash-can keywords into every bucket-like query.
    applied = []
    for zh, en in sorted(SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        if zh in text and not any(zh in a for a in applied):
            keys.extend(en)
            applied.append(zh)
    return [k for k in keys if k]


def locate(memory: SpatialMemory, text: str) -> list[Node]:
    """Naive open-vocabulary locate by label/category/text match.

    Embedding-based retrieval can be plugged in later by comparing
    node.embedding against a query embedding.
    """
    qs = expand_query(text)
    if not qs:
        return []
    hits: list[tuple[Node, float]] = []
    for n in memory.nodes():
        direct_text = " ".join([n.label, n.category, n.layer1_text, n.layer2_text]).lower()
        aliases = " ".join(n.attributes.get("aliases", [])).lower()
        if any(n.label.lower() == q or n.category.lower() == q for q in qs):
            hits.append((n, 3.0))  # exact label/category
        elif any(q in direct_text for q in qs):
            hits.append((n, 2.0))  # substring in label/category/layer text
        elif any(q in aliases for q in qs):
            hits.append((n, 1.0))  # candidate-alias match
    # exact > substring > alias, then confidence as tiebreak
    hits.sort(key=lambda t: (-t[1], -t[0].confidence))
    return [n for n, _ in hits]


def relational_query(
    memory: SpatialMemory,
    subject_hint: str,
    predicate: str,
    object_hint: str,
) -> list[Node]:
    """Follow subject -predicate-> object where both may be partial text.

    The hierarchy edge (parent_id) is treated as an implicit "in" relation:
    a node is "in" its parent (room contains anchor contains object).
    """
    subjects = locate(memory, subject_hint) if subject_hint else memory.nodes()
    objs = locate(memory, object_hint) if object_hint else memory.nodes()
    obj_ids = {o.node_id for o in objs}
    out = []
    for s in subjects:
        if predicate == "in" and s.parent_id in obj_ids:
            out.append(memory.get_node(s.parent_id))
        for r in memory.relations_of(s.node_id):
            if r.status != "active":
                continue
            if predicate and r.predicate != predicate:
                continue
            if r.subject == s.node_id and r.object in obj_ids:
                out.append(memory.get_node(r.object))
    return out


def to_egocentric(
    memory: SpatialMemory,
    node_id: str,
    viewer_pose: np.ndarray,
) -> Optional[dict]:
    node = memory.get_node(node_id)
    if node is None:
        return None
    info = egocentric_direction(node.position(), viewer_pose)
    info["node_id"] = node.node_id
    info["label"] = node.label
    info["layer2"] = node.layer2_text or node.layer1_text
    return info
