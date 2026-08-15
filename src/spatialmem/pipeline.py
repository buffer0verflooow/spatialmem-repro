"""Perception pipeline interfaces (swappable backends, per paper)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .memory import SpatialMemory


@dataclass
class FrameData:
    frame_index: int
    t_s: float
    image: Optional[np.ndarray] = None
    pose: Optional[np.ndarray] = None  # 4x4 world->camera
    depth: Optional[np.ndarray] = None  # metric-ish depth (m)


class GeometryBackend(ABC):
    """Pose + depth from RGB frames. Swappable: VGGT / SLAM3R / COLMAP / device VIO."""

    @abstractmethod
    def process(self, frames: list[FrameData]) -> list[FrameData]:
        """Fill pose and depth for each frame."""


class AnchorExtractor(ABC):
    @abstractmethod
    def extract(self, points: np.ndarray) -> list[dict]:
        """From fused point cloud -> anchors (walls/doors/windows)."""


class ObjectLifter(ABC):
    @abstractmethod
    def lift(self, frame: FrameData, detections: list[dict]) -> list[dict]:
        """2D detections + depth + pose -> 3D boxes."""


class MemoryBuilder:
    """Orchestrates memory updates with confirmation policy."""

    def __init__(self, memory: Optional[SpatialMemory] = None) -> None:
        self.memory = memory or SpatialMemory()

    def ingest_objects(self, objects: list[dict], t_s: float) -> None:
        """Upsert object nodes; attach to parent anchors; confirm relations."""
        for obj in objects:
            node_id = obj.get("node_id")
            if node_id is None:
                node_id = self.memory._next_id("object")
                obj["node_id"] = node_id
            existing = self.memory.get_node(node_id)
            if existing is None:
                self.memory.add_node(
                    node_type="object",
                    node_id=node_id,
                    label=obj.get("label", ""),
                    category=obj.get("category", ""),
                    box=obj.get("box"),
                    attributes=obj.get("attributes", {}),
                    parent_id=obj.get("parent_id"),
                    confidence=obj.get("confidence", 1.0),
                    t_s=t_s,
                )
            else:
                self.memory.touch(node_id, t_s)
                if obj.get("box") is not None:
                    existing.box = obj["box"]
                if obj.get("attributes"):
                    existing.attributes.update(obj["attributes"])
            if obj.get("support_id"):
                self.memory.add_relation(
                    node_id, "on", obj["support_id"], confidence=obj.get("confidence", 1.0), t_s=t_s
                )
            if obj.get("room_id"):
                self.memory.add_relation(
                    node_id, "in", obj["room_id"], confidence=obj.get("confidence", 1.0), t_s=t_s
                )

