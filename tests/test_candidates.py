"""M5.1 候选池 / novelty / 同实例合并测试（合成数据）。"""

import io

import numpy as np
from PIL import Image, ImageDraw

from spatialmem.candidates import (
    CandidatePool,
    appearance_distance,
    crop_feature,
    is_same_object,
    merge_duplicate_instances,
)


def solid_crop(rgb: tuple[int, int, int], size: int = 32) -> np.ndarray:
    return np.full((size, size, 3), rgb, dtype=np.uint8)


def chair_like(dark: bool = True) -> np.ndarray:
    img = Image.new("RGB", (48, 48), (20, 18, 16) if dark else (235, 235, 235))
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, 40, 40], fill=(35, 30, 25) if dark else (200, 200, 200))
    d.rectangle([14, 14, 34, 34], fill=(15, 13, 11) if dark else (245, 245, 245))
    return np.asarray(img)


def test_crop_feature_black_vs_white() -> None:
    f_black = crop_feature(solid_crop((15, 13, 11)))
    f_white = crop_feature(solid_crop((240, 240, 240)))
    assert appearance_distance(f_black, f_white) > 0.5
    f_same = crop_feature(solid_crop((16, 14, 12)))
    assert appearance_distance(f_black, f_same) < 0.05


def test_is_same_object_requires_appearance_and_auxiliary_evidence() -> None:
    chair_a = crop_feature(chair_like(dark=True))
    chair_b = crop_feature(chair_like(dark=True))
    fan = crop_feature(solid_crop((240, 240, 240)))
    # 两把黑椅子：外观相似 + 时间窗重叠 -> 同一物体
    assert is_same_object(chair_a, chair_b, window_a=(540, 610), window_b=(570, 607))
    # 黑椅子 vs 白风扇：外观不相似 -> 不同
    assert not is_same_object(chair_a, fan, window_a=(540, 610), window_b=(570, 607))
    # 外观相似但时间窗不重叠且 3D 很远 -> 不同
    assert not is_same_object(
        chair_a,
        chair_b,
        window_a=(100, 200),
        window_b=(700, 800),
        center_a=(0, 0, 0),
        center_b=(5, 5, 5),
    )


def test_is_same_object_semantic_priority() -> None:
    fa = crop_feature(chair_like(dark=True))
    fb = crop_feature(chair_like(dark=True))
    # 语义相同（黑椅子）：颜色一致 + 名称族一致 → 同一物体，即使外观特征缺失也无妨
    assert is_same_object(fa, fb, semantic_a=("椅子", "黑色"), semantic_b=("办公椅", "黑色"))
    # 颜色不同 → 不同（黑椅子 vs 白风扇）
    assert not is_same_object(
        fa, fb, semantic_a=("椅子", "黑色"), semantic_b=("风扇", "白色")
    )
    # 同色但名称族不同且无几何/时间证据 → 保守判不同
    assert not is_same_object(
        fa, fb, semantic_a=("风扇", "白色"), semantic_b=("马桶", "白色")
    )


def test_candidate_pool_novelty_and_merge() -> None:
    pool = CandidatePool(
        known_features=[("chair_6", crop_feature(chair_like(dark=True)))],
        known_semantics=[("chair_6", ("椅子", "黑色"))],
    )
    # 已知黑椅子的残留检测 -> 跳过（known）
    cid = pool.add_or_merge(
        feature=crop_feature(chair_like(dark=True)),
        frame="frame_000613.jpg",
        bbox=[578, 0, 843, 208],
        label_hint="chair",
        semantic=("椅子", "黑色"),
    )
    assert cid == ""
    assert pool.stats["known_skip"] == 1
    # 白风扇 -> 新候选
    fan_id = pool.add_or_merge(
        feature=crop_feature(solid_crop((240, 240, 240))),
        frame="frame_000619.jpg",
        bbox=[483, 362, 755, 462],
        label_hint="chair",
        semantic=("风扇", "白色"),
    )
    assert fan_id != ""
    assert pool.stats["new"] == 1
    # 同风扇再次出现 -> 合并
    pool.add_or_merge(
        feature=crop_feature(solid_crop((238, 238, 238))),
        frame="frame_000628.jpg",
        bbox=[510, 231, 797, 346],
        label_hint="chair",
        semantic=("风扇", "白色"),
    )
    assert pool.stats["merged"] == 1
    cands = pool.candidates()
    assert len(cands) == 1
    assert cands[0].n_observations == 2


def test_merge_duplicate_instances(tmp_path) -> None:
    """同一黑椅子两条实例（外观相同、窗口重叠）应合并。"""
    import json

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    img = Image.new("RGB", (640, 360), (20, 18, 16))
    d = ImageDraw.Draw(img)
    d.rectangle([360, 0, 620, 170], fill=(35, 30, 25))
    for f in ("frame_000586.jpg", "frame_000595.jpg"):
        img.save(frames_dir / f)
    instances = [
        {
            "instance_id": "chair_6",
            "class": "chair",
            "center": [1.98, -1.51, 0.27],
            "median_conf": 0.833,
            "n_observations": 6,
            "first_frame": "frame_000540.jpg",
            "last_frame": "frame_000610.jpg",
            "evidence": {"frame": "frame_000586.jpg", "bbox2d": [368, -1, 619, 170]},
        },
        {
            "instance_id": "chair_8",
            "class": "chair",
            "center": [2.09, -2.38, 0.8],
            "median_conf": 0.761,
            "n_observations": 6,
            "first_frame": "frame_000570.jpg",
            "last_frame": "frame_000607.jpg",
            "evidence": {"frame": "frame_000595.jpg", "bbox2d": [395, 27, 646, 272]},
        },
    ]
    merged, report = merge_duplicate_instances(instances, frames_dir=frames_dir)
    # 无语义时外观+时间窗也应能合并（兜底路径）
    assert len(merged) == 1
    assert merged[0]["instance_id"] == "chair_6"
    assert merged[0]["merged_from"] == ["chair_8"]
    assert merged[0]["n_observations"] == 12
    assert len(report) == 1


def test_merge_duplicate_instances_semantic(tmp_path) -> None:
    """语义一致（黑椅子）时应合并；颜色不同（黑椅 vs 白风扇）不合并。"""
    from PIL import Image, ImageDraw

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    img = Image.new("RGB", (640, 360), (20, 18, 16))
    d = ImageDraw.Draw(img)
    d.rectangle([360, 0, 620, 170], fill=(35, 30, 25))
    for f in ("f1.jpg", "f2.jpg"):
        img.save(frames_dir / f)
    base = {
        "class": "chair",
        "center": [2.0, -2.0, 0.5],
        "median_conf": 0.8,
        "n_observations": 6,
        "first_frame": "frame_000540.jpg",
        "last_frame": "frame_000610.jpg",
    }
    a = dict(base, instance_id="chair_6",
             evidence={"frame": "f1.jpg", "bbox2d": [360, 0, 620, 170]})
    b = dict(base, instance_id="chair_8", median_conf=0.7,
             first_frame="frame_000570.jpg", last_frame="frame_000607.jpg",
             evidence={"frame": "f2.jpg", "bbox2d": [360, 0, 620, 170]})
    sem = {"chair_6": ("椅子", "黑色"), "chair_8": ("办公椅", "黑色")}
    merged, _ = merge_duplicate_instances([a, b], frames_dir=frames_dir, semantics=sem)
    assert len(merged) == 1

    # 颜色不同（一个黑椅一个白风扇）→ 不合并
    sem2 = {"chair_6": ("椅子", "黑色"), "chair_8": ("风扇", "白色")}
    merged2, _ = merge_duplicate_instances([a, b], frames_dir=frames_dir, semantics=sem2)
    assert len(merged2) == 2
