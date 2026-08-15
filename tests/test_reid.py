"""M5.4 再识别 / 遗忘 / 纠正测试。"""

import numpy as np

from spatialmem.memory_lifecycle import apply_correction, move_node, stale_status
from spatialmem.reid import ReidEntry, reidentify


def test_strong_match_via_semantics() -> None:
    r = reidentify(
        np.zeros(4),
        ("办公椅", "黑色"),
        [ReidEntry("chair_6", None, ("椅子", "黑色"))],
    )
    assert r.tier == "strong"
    assert r.match_id == "chair_6"


def test_weak_match_via_appearance() -> None:
    fa = np.array([1.0, 0.0, 0.0, 0.0])
    fb = np.array([0.99, 0.01, 0.0, 0.0])
    r = reidentify(
        fa,
        ("马桶", "白色"),
        [ReidEntry("fan", fb, ("电风扇", "白色"))],
        sim_threshold=0.95,
    )
    assert r.tier == "weak"
    assert r.match_id == "fan"


def test_no_match_when_color_contradicts() -> None:
    fa = np.array([1.0, 0.0, 0.0, 0.0])
    r = reidentify(
        fa,
        ("风扇", "白色"),
        [ReidEntry("chair", fa, ("椅子", "黑色"))],
        sim_threshold=0.95,
    )
    assert r.tier == "none"


def test_correction_updates_label_and_downgrades_confidence() -> None:
    node = {"candidate_id": "cand_35", "name": "电风扇", "color": "白色",
            "confidence": 1.0, "sources": ["interactive"]}
    new, log = apply_correction(node, name="暖风机", t_s=1.0)
    assert log.old_name == "电风扇"
    assert new["name"] == "暖风机"
    assert new["confidence"] == 0.5
    assert "interactive" in new["sources"]


def test_move_archives_old_position() -> None:
    node = {"candidate_id": "cand_35", "center": [1.0, 2.0, 0.0]}
    new, archived = move_node(node, [3.0, 4.0, 0.0], t_s=10.0)
    assert new["center"] == [3.0, 4.0, 0.0]
    assert archived is not None
    assert archived.old_center == [1.0, 2.0, 0.0]
    assert archived.archived_at_s == 10.0


def test_stale_status_by_age() -> None:
    nodes = [
        {"node_id": "a", "last_seen_s": 100.0},
        {"node_id": "b", "last_seen_s": 1_000_000.0},
    ]
    status = stale_status(nodes, now_s=1_003_000.0, max_age_s=3600.0)
    assert status == {"a": "stale", "b": "active"}
