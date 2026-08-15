"""M3 描述层测试：颜色/尺寸/视图文本/Layer2 合并/关系文本/位姿。"""

import numpy as np

from spatialmem.descriptions import (
    DescriptionAccumulator,
    box_dims_cm,
    dominant_color_name,
    pose_from_quat,
    relation_text,
    size_text_cm,
    view_description,
    zh_direction,
    zh_label,
)
from spatialmem.memory import SpatialMemory


def solid_rgb(rgb: tuple[int, int, int]) -> np.ndarray:
    return np.full((32, 32, 3), rgb, dtype=np.uint8)


def test_color_naming() -> None:
    assert dominant_color_name(solid_rgb((220, 40, 40))) == "红色"
    assert dominant_color_name(solid_rgb((40, 120, 230))) == "蓝色"
    assert dominant_color_name(solid_rgb((245, 245, 245))) == "白色"
    assert dominant_color_name(solid_rgb((10, 10, 10))) == "黑色"
    assert dominant_color_name(solid_rgb((130, 130, 130))) == "深灰色"


def test_size_from_metric_box() -> None:
    box = (0.0, 0.0, 0.72, 0.09, 0.08, 0.78)
    assert box_dims_cm(box) == (9, 8, 6)
    assert size_text_cm(box) == "约 9×8×6 厘米"


def test_view_description_format() -> None:
    text = view_description(
        "杯子",
        color="白色",
        box=(0.0, 0.0, 0.72, 0.09, 0.08, 0.78),
        direction_tag="左前方",
        distance_m=1.25,
    )
    assert text == "白色的杯子，约 9×8×6 厘米，在你左前方约 1.2 米"
    assert view_description("杯子", color=None, box=None, direction_tag=None, distance_m=None) == "杯子"


def test_layer2_consensus_requires_agreement() -> None:
    acc = DescriptionAccumulator(window=3, min_confirmations=2)
    # 两次一致 -> 颜色写入
    acc.observe("cup_1", "白色", (9, 8, 6))
    acc.observe("cup_1", "白色", (9, 8, 6))
    assert acc.layer2("cup_1", "杯子") == "白色的杯子，约 9×8×6 厘米"
    # 第三次出现分歧 -> 颜色不再写入（保守）
    acc.observe("cup_1", "蓝色", (9, 8, 6))
    assert "白色" not in acc.layer2("cup_1", "杯子")
    assert "约 9×8×6 厘米" in acc.layer2("cup_1", "杯子")


def test_layer2_size_uses_median() -> None:
    acc = DescriptionAccumulator(window=3, min_confirmations=2)
    for size in [(10, 8, 6), (12, 8, 6), (11, 8, 6)]:
        acc.observe("cup_1", "白色", size)
    assert acc.layer2("cup_1", "杯子") == "白色的杯子，约 11×8×6 厘米"


def test_layer2_single_view_writes_nothing() -> None:
    acc = DescriptionAccumulator(min_confirmations=2)
    acc.observe("cup_1", "白色", (9, 8, 6))
    assert acc.layer2("cup_1", "杯子") == ""


def test_relation_text_uses_active_relations() -> None:
    mem = SpatialMemory()
    room = mem.add_node("room", label="室内", t_s=0.0)
    desk = mem.add_node("anchor", label="桌面", category="support_surface",
                        parent_id=room.node_id, t_s=0.0)
    cup = mem.add_node("object", label="杯子", parent_id=room.node_id, t_s=0.0)
    mem.add_relation(cup.node_id, "on", desk.node_id, confidence=0.9, t_s=0.0)
    mem.add_relation(cup.node_id, "in", room.node_id, confidence=0.95, t_s=0.0)
    text = relation_text(mem, cup.node_id)
    assert "在桌面上" in text
    assert "在室内" not in text  # 房间节点不展开


def test_relation_text_excludes_incoming_relations() -> None:
    mem = SpatialMemory()
    room = mem.add_node("room", label="室内", t_s=0.0)
    laptop = mem.add_node("object", label="laptop", parent_id=room.node_id, t_s=0.0)
    cup = mem.add_node("object", label="cup", parent_id=room.node_id, t_s=0.0)
    mem.add_relation(laptop.node_id, "above", cup.node_id, confidence=0.8, t_s=0.0)
    # 入边「笔记本电脑在杯子上方」不应出现在杯子的描述里
    assert relation_text(mem, cup.node_id) == ""
    # 出边翻译为中文
    assert relation_text(mem, laptop.node_id) == "在杯子上方"


def test_zh_maps() -> None:
    assert zh_label("laptop") == "笔记本电脑"
    assert zh_label("unknown_class") == "unknown_class"
    assert zh_direction("left_front") == "左前方"
    assert zh_direction(None) is None


def test_pose_from_quat_identity_translates() -> None:
    pose = pose_from_quat(0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 0.5)
    p = np.array([3.0, 2.0, 0.5])
    local = pose[:3, :3].T @ (p - pose[:3, 3])
    np.testing.assert_allclose(local, [2.0, 0.0, 0.0], atol=1e-9)
