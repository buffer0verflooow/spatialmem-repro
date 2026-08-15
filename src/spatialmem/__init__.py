"""SpatialMem reproduction: object-relation spatial memory from egocentric video."""

from .memory import Node, Relation, SpatialMemory
from .relations import (
    box_bottom_z,
    box_top_z,
    box_center,
    footprint_overlap,
    predicate_on,
    predicate_above_below,
    predicate_near,
    predicate_contains,
    egocentric_direction,
)
from .query import locate, relational_query, to_egocentric

__all__ = [
    "Node",
    "Relation",
    "SpatialMemory",
    "box_bottom_z",
    "box_top_z",
    "box_center",
    "footprint_overlap",
    "predicate_on",
    "predicate_above_below",
    "predicate_near",
    "predicate_contains",
    "egocentric_direction",
    "locate",
    "relational_query",
    "to_egocentric",
]

