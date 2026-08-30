"""Tests for the P1-c anchor relation evaluation harness."""

from spatialmem.anchor_eval import (
    Anchor,
    evaluate_anchors,
    relation_support,
)


def classroom_anchors() -> list[Anchor]:
    return [
        Anchor("door", "门", "right", 6.1),
        Anchor("window", "窗户", "front", 5.2),
        Anchor("wall", "墙", "left", 3.0),
    ]


def test_perfect_match_gives_score_one() -> None:
    gt = classroom_anchors()
    rep = evaluate_anchors(gt, gt)
    assert rep["relation_score"] == 1.0
    assert rep["per_type"]["door"]["f1"] == 1.0
    assert rep["per_type"]["window"]["f1"] == 1.0
    assert rep["per_type"]["wall"]["f1"] == 1.0


def test_direction_mismatch_penalizes_that_type() -> None:
    gt = classroom_anchors()
    pred = [
        Anchor("door", "门", "left", 6.1),  # 方向错 → door tp=0
        Anchor("window", "窗户", "front", 5.2),
        Anchor("wall", "墙", "left", 3.0),
    ]
    rep = evaluate_anchors(pred, gt)
    assert rep["per_type"]["door"]["f1"] == 0.0
    assert rep["per_type"]["window"]["f1"] == 1.0
    assert rep["per_type"]["wall"]["f1"] == 1.0
    assert abs(rep["relation_score"] - 2 / 3) < 1e-9


def test_distance_tolerance_controls_match() -> None:
    gt = [Anchor("door", "门", "right", 6.1)]
    pred = [Anchor("door", "门", "right", 9.0)]  # 差 2.9m
    assert evaluate_anchors(pred, gt, distance_tol_m=2.0)["per_type"]["door"]["f1"] == 0.0
    assert evaluate_anchors(pred, gt, distance_tol_m=3.0)["per_type"]["door"]["f1"] == 1.0


def test_relation_support_requires_both_endpoints() -> None:
    gt = classroom_anchors()
    rels = [{"subject": "窗户", "predicate": "near", "object": "墙"}]
    assert relation_support(gt, gt, rels) == 1.0
    only_door = [Anchor("door", "门", "right", 6.1)]
    assert relation_support(only_door, gt, rels) == 0.0


def test_require_direction_can_be_disabled() -> None:
    gt = [Anchor("door", "门", "right", 6.1)]
    pred = [Anchor("door", "门", "left", 6.1)]
    assert evaluate_anchors(pred, gt, require_direction=True)["per_type"]["door"]["f1"] == 0.0
    assert evaluate_anchors(pred, gt, require_direction=False)["per_type"]["door"]["f1"] == 1.0
