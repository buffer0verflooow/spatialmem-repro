"""Tests for multi-hop relational query and the visibility predicate."""

import numpy as np

from spatialmem.memory import SpatialMemory
from spatialmem.query import multi_hop_query
from spatialmem.relations import predicate_visible


def build_wall_window_mug() -> SpatialMemory:
    mem = SpatialMemory()
    t = 0.0
    room = mem.add_node(
        "room",
        label="房间",
        category="room",
        box=(0.0, 0.0, 0.0, 8.0, 8.0, 3.0),
        t_s=t,
    )
    wall = mem.add_node(
        "anchor",
        label="墙",
        category="wall",
        box=(7.5, 0.0, 0.0, 8.0, 8.0, 3.0),
        parent_id=room.node_id,
        t_s=t,
    )
    window = mem.add_node(
        "anchor",
        label="窗户",
        category="window",
        box=(7.5, 3.0, 1.0, 8.0, 4.0, 2.2),
        parent_id=room.node_id,
        t_s=t,
    )
    mug = mem.add_node(
        "object",
        label="杯子",
        category="mug",
        box=(6.4, 3.0, 0.9, 6.7, 3.3, 1.1),
        parent_id=room.node_id,
        t_s=t,
    )
    # 论文 §3.4 的 wall → window → mug 多跳链。
    mem.add_relation(wall.node_id, "near", window.node_id, t_s=t)
    mem.add_relation(window.node_id, "near", mug.node_id, t_s=t)
    return mem


def test_multi_hop_wall_window_mug() -> None:
    mem = build_wall_window_mug()
    hits = multi_hop_query(mem, "墙", ["near", "near"], "杯子")
    assert len(hits) == 1
    assert hits[0].label == "杯子"


def test_multi_hop_returns_empty_on_broken_chain() -> None:
    mem = build_wall_window_mug()
    assert multi_hop_query(mem, "墙", ["near", "above"], "杯子") == []


def test_multi_hop_filters_by_end_hint() -> None:
    mem = build_wall_window_mug()
    # 链条能走到，但 end_hint 不匹配时返回空。
    assert multi_hop_query(mem, "墙", ["near", "near"], "桌子") == []


def test_visibility_unoccluded() -> None:
    target = (6.5, 3.0, 0.9, 6.8, 3.3, 1.1)
    viewer = np.array([0.0, 0.0, 1.5])
    assert predicate_visible(viewer, target, [])


def test_visibility_occluded_by_intervening_box() -> None:
    target = (6.5, 3.0, 0.9, 6.8, 3.3, 1.1)
    viewer = np.array([0.0, 0.0, 1.5])
    occluder = (3.0, 1.3, 0.0, 4.0, 1.7, 1.5)
    assert not predicate_visible(viewer, target, [occluder])


def test_visibility_ignores_occluder_behind_target() -> None:
    target = (6.5, 3.0, 0.9, 6.8, 3.3, 1.1)
    viewer = np.array([0.0, 0.0, 1.5])
    behind = (7.0, 3.0, 0.9, 7.5, 3.3, 1.1)
    assert predicate_visible(viewer, target, [behind])
