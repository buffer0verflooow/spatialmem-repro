"""稠密地图测试：深度反投影、多帧融合、地板高度估计。"""

import numpy as np

from spatialmem.dense_map import (
    backproject_depth,
    dense_occupancy,
    estimate_floor_z,
    fuse_dense_cloud,
)


def make_camera_look_down(z_cam: float = 1.5) -> np.ndarray:
    """world->camera：相机在 (0,0,z_cam) 垂直俯视（camera +z = 世界 -z）。"""
    R = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]])
    t = -(R @ np.array([0.0, 0.0, z_cam]))
    pose = np.eye(4)
    pose[:3, :3] = R
    pose[:3, 3] = t
    return pose


def synthetic_depth() -> tuple[np.ndarray, np.ndarray]:
    """合成 160x120 深度图：正下方地板（z=0，深度 1.5m）+ 中央小立柱（0.8m）。"""
    h, w = 120, 160
    depth = np.full((h, w), 1.5, dtype=np.float32)
    depth[50:65, 70:85] = 0.8
    return depth, make_camera_look_down()


def test_backproject_depth_produces_world_points():
    depth, pose = synthetic_depth()
    pts = backproject_depth(depth, pose, (200.0, 80.0, 60.0, 0.0), pixel_step=8)
    assert len(pts) > 100
    # 相机在 z=1.5 俯视地板 → 地板点反投影回 z≈0
    assert abs(float(np.median(pts[:, 2]))) < 0.15
    # 立柱点（深度 0.8）应落在 z≈0.7
    assert np.any((pts[:, 2] > 0.6) & (pts[:, 2] < 0.8))


def test_fuse_dense_cloud_accumulates_frames():
    depth, pose = synthetic_depth()
    poses = np.stack([pose, pose])
    cloud = fuse_dense_cloud(poses, (200.0, 80.0, 60.0, 0.0), [depth, depth], pixel_step=8)
    single = backproject_depth(depth, pose, (200.0, 80.0, 60.0, 0.0), pixel_step=8)
    assert len(cloud) == 2 * len(single)


def test_estimate_floor_z_ignores_table_top():
    # 地板 2 万点 + 桌面（0.75m）10 万点：桌面更密集，但不能带偏地板估计
    rng = np.random.default_rng(0)
    floor = np.stack(
        [
            rng.uniform(-2, 2, 20_000),
            rng.uniform(-2, 2, 20_000),
            rng.normal(0.0, 0.01, 20_000),
        ],
        axis=1,
    )
    table = np.stack(
        [
            rng.uniform(-0.8, 0.8, 100_000),
            rng.uniform(-0.5, 0.5, 100_000),
            rng.normal(0.75, 0.01, 100_000),
        ],
        axis=1,
    )
    floor_z = estimate_floor_z(np.vstack([floor, table]))
    assert abs(floor_z) < 0.05, f"地板高度估计被桌面带偏: {floor_z}"


def test_dense_occupancy_floor_evidence():
    rng = np.random.default_rng(1)
    floor = np.stack(
        [
            rng.uniform(-3, 3, 30_000),
            rng.uniform(-3, 3, 30_000),
            rng.normal(0.0, 0.01, 30_000),
        ],
        axis=1,
    )
    box = np.stack(
        [
            rng.uniform(-0.3, 0.3, 5_000),
            rng.uniform(-0.3, 0.3, 5_000),
            rng.uniform(0.1, 0.6, 5_000),
        ],
        axis=1,
    )
    grid, floor_z = dense_occupancy(np.vstack([floor, box]))
    assert abs(floor_z) < 0.05
    assert grid.floor_evidence.sum() > 0
    # 障碍格必须存在（中间的箱子）
    assert grid.occ.sum() > 0
