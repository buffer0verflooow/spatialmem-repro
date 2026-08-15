"""builder 装配测试：room/anchor/support/object 节点与 on/above/near/in 关系。"""

import numpy as np

from spatialmem.builder import build_memory_from_artifacts


def test_builder_assembles_hierarchy_and_on_relations() -> None:
    points = np.array(
        [
            [0.0, 0.0, 0.0],
            [5.0, 4.0, 2.5],
            [2.0, 2.0, 0.78],
            [2.5, 2.5, 0.78],
        ]
    )
    anchors = [
        {"anchor_id": "wall_0", "category": "wall", "box": (0, 0, 0, 5, 0.2, 2.5), "confidence": 1.0}
    ]
    supports = [
        {
            "support_id": "support_0",
            "category": "support_surface",
            "top_z": 0.78,
            "box": (1.0, 1.0, 0.68, 3.0, 3.0, 0.78),
        }
    ]
    instances = [
        {
            "instance_id": "cup_1",
            "class": "cup",
            "box3d": (2.0, 2.0, 0.78, 2.1, 2.1, 0.9),
            "n_observations": 3,
            "median_conf": 0.8,
        },
        {
            "instance_id": "laptop_0",
            "class": "laptop",
            "box3d": (2.3, 2.3, 0.78, 2.9, 2.7, 0.81),
            "n_observations": 5,
            "median_conf": 0.9,
        },
    ]
    mem = build_memory_from_artifacts(
        anchors=anchors,
        supports=supports,
        instances=instances,
        points_metric=points,
        on_z_tol=0.05,
    )
    room = next(n for n in mem.nodes() if n.node_type == "room")
    wall = mem.get_node("wall_0")
    support = mem.get_node("support_0")
    cup = mem.get_node("cup_1")
    laptop = mem.get_node("laptop_0")
    assert wall.parent_id == room.node_id
    assert support.parent_id == room.node_id
    assert cup.parent_id == room.node_id
    assert laptop.parent_id == room.node_id

    rels = {r.key(): r for r in mem.active_relations()}
    assert ("cup_1", "on", "support_0") in rels
    assert ("laptop_0", "on", "support_0") in rels
    assert ("wall_0", "in", room.node_id) in rels
    assert ("support_0", "in", room.node_id) in rels
    # 杯在笔记本上方（z 差）
    assert ("cup_1", "above", "laptop_0") in rels


def test_builder_without_supports_still_assembles() -> None:
    points = np.array([[0.0, 0.0, 0.0], [4.0, 3.0, 2.0]])
    mem = build_memory_from_artifacts(
        anchors=[],
        supports=None,
        instances=[],
        points_metric=points,
    )
    assert len([n for n in mem.nodes() if n.node_type == "room"]) == 1
