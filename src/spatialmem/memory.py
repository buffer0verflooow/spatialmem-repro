"""Hierarchical spatial memory: room -> anchors -> objects -> descriptions.

Data model follows SpatialMem (arXiv:2601.14895v2):
- rooted tree in an upright, metric 3D frame (z up, floor plane at z=0)
- each node stores geometry G(v), semantics S(v), text D(v)
- edges are parent-child (hierarchy) or typed spatial relations
- every object carries a two-layer description:
  Layer 1 (image-level): tied to the current frame, changes with viewpoint
  Layer 2 (scene-level): written only after multi-view agreement (conservative)
- relations keep confidence + timestamps + view tags; stale relations are
  archived (with timestamp) instead of silently deleted
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Node:
    node_id: str
    node_type: str  # room | anchor | object | description
    label: str = ""
    category: str = ""  # open-vocabulary semantic category
    box: Optional[tuple] = None  # (xmin, ymin, zmin, xmax, ymax, zmax) metric
    center: Optional[tuple] = None  # (x, y, z) override; derived from box if None
    embedding: Optional[list] = None  # visual/CLIP embedding (optional)
    attributes: dict = field(default_factory=dict)
    layer1_text: str = ""  # image-level description (current view)
    layer2_text: str = ""  # scene-level stable description
    first_seen_s: float = 0.0
    last_seen_s: float = 0.0
    last_updated_s: float = 0.0
    confidence: float = 1.0
    evidence: list = field(default_factory=list)  # evidence patch refs
    status: str = "active"  # active | stale | archived
    parent_id: Optional[str] = None  # hierarchy edge (room -> anchor -> object)

    def position(self) -> tuple:
        if self.center is not None:
            return tuple(self.center)
        if self.box is not None:
            xmin, ymin, zmin, xmax, ymax, zmax = self.box
            return ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)
        return (0.0, 0.0, 0.0)


@dataclass
class Relation:
    subject: str
    predicate: str  # on | in | above | below | near | left_of | right_of | front_of | behind | ...
    object: str
    confidence: float = 1.0
    first_seen_s: float = 0.0
    last_seen_s: float = 0.0
    view_tags: dict = field(default_factory=dict)
    status: str = "active"  # active | archived
    confirmations: int = 1  # multi-frame confirmation count

    def key(self) -> tuple:
        return (self.subject, self.predicate, self.object)


class SpatialMemory:
    """Rooted-tree spatial memory with typed relations."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._relations: dict[tuple, Relation] = {}
        self._archived: list[Relation] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def add_node(
        self,
        node_type: str,
        label: str = "",
        category: str = "",
        box: Optional[tuple] = None,
        center: Optional[tuple] = None,
        embedding: Optional[list] = None,
        attributes: Optional[dict] = None,
        parent_id: Optional[str] = None,
        confidence: float = 1.0,
        t_s: float = 0.0,
        node_id: Optional[str] = None,
        evidence: Optional[list] = None,
    ) -> Node:
        node = Node(
            node_id=node_id or self._next_id(node_type),
            node_type=node_type,
            label=label,
            category=category,
            box=box,
            center=center,
            embedding=embedding,
            attributes=attributes or {},
            first_seen_s=t_s,
            last_seen_s=t_s,
            last_updated_s=t_s,
            confidence=confidence,
            parent_id=parent_id,
            evidence=evidence or [],
        )
        self._nodes[node.node_id] = node
        return node

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def node_by_label(self, label: str) -> Optional[Node]:
        for n in self._nodes.values():
            if n.label == label:
                return n
        return None

    def touch(self, node_id: str, t_s: float) -> None:
        node = self._nodes.get(node_id)
        if node is not None:
            node.last_seen_s = max(node.last_seen_s, t_s)
            node.last_updated_s = t_s

    def children_of(self, node_id: str) -> list[Node]:
        return [n for n in self._nodes.values() if n.parent_id == node_id]

    def relations_of(self, node_id: str) -> list[Relation]:
        out = []
        for r in self._relations.values():
            if r.subject == node_id or r.object == node_id:
                out.append(r)
        return out

    def add_relation(
        self,
        subject: str,
        predicate: str,
        object_id: str,
        confidence: float = 1.0,
        t_s: float = 0.0,
        view_tags: Optional[dict] = None,
        min_confirmations: int = 1,
    ) -> Relation:
        """Upsert a relation. Event-driven updates with confirmation counting."""
        key = (subject, predicate, object_id)
        existing = self._relations.get(key)
        if existing is not None:
            existing.confidence = max(existing.confidence, confidence)
            existing.last_seen_s = max(existing.last_seen_s, t_s)
            existing.confirmations += 1
            if view_tags:
                existing.view_tags.update(view_tags)
            return existing
        rel = Relation(
            subject=subject,
            predicate=predicate,
            object=object_id,
            confidence=confidence,
            first_seen_s=t_s,
            last_seen_s=t_s,
            view_tags=view_tags or {},
        )
        if rel.confirmations >= min_confirmations:
            self._relations[key] = rel
        else:
            # keep candidate out of the active set until confirmed
            rel.status = "archived"
            self._archived.append(rel)
        return rel

    def archive_relation(self, subject: str, predicate: str, object_id: str, t_s: float) -> None:
        key = (subject, predicate, object_id)
        rel = self._relations.pop(key, None)
        if rel is not None:
            rel.status = "archived"
            rel.last_seen_s = t_s
            self._archived.append(rel)

    def archived_relations_of(self, node_id: str) -> list[Relation]:
        return [
            r
            for r in self._archived
            if r.subject == node_id or r.object == node_id
        ]

    def active_relations(self) -> list[Relation]:
        return list(self._relations.values())

    def __repr__(self) -> str:
        return f"<SpatialMemory nodes={len(self._nodes)} relations={len(self._relations)}>"
