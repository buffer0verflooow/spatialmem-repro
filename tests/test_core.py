"""Smoke tests for the core memory logic (synthetic data)."""

import numpy as np

from spatialmem.memory import SpatialMemory
from spatialmem.query import locate, relational_query, to_egocentric
from spatialmem.relations import (
    predicate_contains,
    predicate_near,
    predicate_on,
)


def build_scene() -> SpatialMemory:
    mem = SpatialMemory()
    t = 0.0
    room = mem.add_node(
        "room",
        label="客厅",
        category="living_room",
        box=(0.0, 0.0, 0.0, 5.0, 4.0, 3.0),
        t_s=t,
    )
    table = mem.add_node(
        "object",
        label="桌子",
        category="table",
        box=(1.5, 1.0, 0.7, 3.5, 3.0, 0.78),
        parent_id=room.node_id,
        t_s=t,
    )
    quilt = mem.add_node(
        "object",
        label="被子",
        category="quilt",
        box=(2.0, 1.5, 0.78, 2.8, 2.5, 0.95),
        parent_id=table.node_id,
        t_s=t,
    )
    cup = mem.add_node(
        "object",
        label="杯子",
        category="cup",
        box=(2.9, 2.6, 0.78, 3.1, 2.8, 0.98),
        parent_id=table.node_id,
        t_s=t,
    )
    window = mem.add_node(
        "anchor",
        label="窗户",
        category="window",
        box=(4.7, 0.0, 1.0, 5.0, 2.0, 2.2),
        parent_id=room.node_id,
        t_s=t,
    )
    mem.add_relation(quilt.node_id, "on", table.node_id, t_s=t)
    mem.add_relation(cup.node_id, "on", table.node_id, t_s=t)
    mem.add_relation(table.node_id, "near", window.node_id, t_s=t)
    return mem


def test_predicates_on_synthetic_scene() -> None:
    mem = build_scene()
    quilt = mem.node_by_label("被子")
    table = mem.node_by_label("桌子")
    room = mem.node_by_label("客厅")
    cup = mem.node_by_label("杯子")
    assert predicate_on(quilt.box, table.box)
    assert predicate_on(cup.box, table.box)
    assert predicate_contains(room.box, table.box)
    assert not predicate_on(table.box, room.box)


def test_locate_and_relational_query() -> None:
    mem = build_scene()
    hits = locate(mem, "被子")
    assert len(hits) == 1 and hits[0].label == "被子"
    on_table = relational_query(mem, "被子", "on", "桌子")
    assert len(on_table) == 1 and on_table[0].label == "桌子"
    in_room = relational_query(mem, "桌子", "in", "客厅")
    assert len(in_room) == 1 and in_room[0].label == "客厅"


def test_egocentric_answer() -> None:
    mem = build_scene()
    quilt = mem.node_by_label("被子")
    # viewer stands at origin facing +x (identity pose, forward = +x)
    pose = np.eye(4)
    ans = to_egocentric(mem, quilt.node_id, pose)
    assert ans is not None
    assert ans["distance"] > 2.0
    assert ans["tag"] == "front"


def test_relation_update_and_archive() -> None:
    mem = build_scene()
    quilt = mem.node_by_label("被子")
    table = mem.node_by_label("桌子")
    sofa = mem.add_node(
        "object",
        label="沙发",
        category="sofa",
        box=(0.0, 3.0, 0.4, 1.2, 4.0, 0.5),
        parent_id=mem.node_by_label("客厅").node_id,
        t_s=10.0,
    )
    # user moved quilt from table to sofa
    mem.archive_relation(quilt.node_id, "on", table.node_id, t_s=11.0)
    mem.add_relation(quilt.node_id, "on", sofa.node_id, t_s=11.0)
    active = mem.active_relations()
    assert any(r.predicate == "on" and r.object == sofa.node_id for r in active)
    archived = mem.archived_relations_of(quilt.node_id)
    assert any(r.predicate == "on" and r.object == table.node_id for r in archived)

