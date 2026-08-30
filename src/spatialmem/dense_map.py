"""稠密度量地图：MiDaS 米制深度 × COLMAP 位姿 → 稠密点云 → 地板/可通行图。

P0-1b：COLMAP 稀疏点云地板点极少（地板无纹理），无法直接建可通行图；
这里用每帧米制深度图（run_depth.py 已按 COLMAP 稀疏点标定尺度）反投影
并融合成稠密点云，地板因此被稠密覆盖，可构建真正的占用网格。

位姿约定与 projection.py 一致：4x4 world->camera（cam = R @ world + t），
反投影：world = R.T @ (X_c - t)。
"""

from __future__ import annotations

import numpy as np

from .map2d import OccupancyGrid


def backproject_depth(
    depth: np.ndarray,
    pose: np.ndarray,
    intrinsics: tuple[float, float, float, float],
    *,
    pixel_step: int = 2,
    min_depth: float = 0.2,
    max_depth: float = 6.0,
) -> np.ndarray:
    """单帧深度图 → 米制世界坐标点云（(N,3)）。"""
    f, cx, cy, _ = intrinsics
    h, w = depth.shape[:2]
    ys, xs = np.mgrid[0:h:pixel_step, 0:w:pixel_step]
    d = depth[ys, xs].astype(np.float32)
    valid = np.isfinite(d) & (d >= min_depth) & (d <= max_depth)
    if not valid.any():
        return np.zeros((0, 3), dtype=np.float32)
    xs, ys, d = xs[valid], ys[valid], d[valid]
    x_c = (xs.astype(np.float32) - cx) / f * d
    y_c = (ys.astype(np.float32) - cy) / f * d
    z_c = d
    cam = np.stack([x_c, y_c, z_c], axis=1)  # (N,3) camera coords
    R, t = pose[:3, :3], pose[:3, 3]
    world = (cam - t) @ R  # world = R.T @ (cam - t)
    return world.astype(np.float32)


def fuse_dense_cloud(
    poses: np.ndarray,
    intrinsics: tuple[float, float, float, float],
    depth_maps: list[np.ndarray],
    *,
    pixel_step: int = 2,
    max_depth: float = 6.0,
    min_depth: float = 0.2,
) -> np.ndarray:
    """多帧深度×位姿融合 → 稠密米制点云（(N,3)）。"""
    chunks: list[np.ndarray] = []
    for pose, depth in zip(poses, depth_maps):
        pts = backproject_depth(
            depth,
            pose,
            intrinsics,
            pixel_step=pixel_step,
            min_depth=min_depth,
            max_depth=max_depth,
        )
        if len(pts):
            chunks.append(pts)
    if not chunks:
        return np.zeros((0, 3), dtype=np.float32)
    return np.vstack(chunks)


def estimate_floor_z(
    points: np.ndarray,
    *,
    z_min: float = -0.25,
    z_max: float = 0.6,
    min_frac: float = 0.05,
) -> float:
    """稠密点云首个显著频带 = 地板高度（米）。

    不用全局直方图峰值：桌面/台面往往比地板更密集（相机会更多地对着
    桌面），峰值会被台面带偏；也不取最低分位：深度异常值（相机下方
    的错误负 z）会拉低中位数。做法：在低频段（z_min~z_max）里找
    第一个计数达到峰值 [min_frac] 的频带——稀疏异常值被跳过，真正
    的地板面（通常是最低的显著水平面）被选中。
    """
    z = points[:, 2]
    mask = (z >= z_min) & (z <= z_max)
    if not mask.any():
        return float(np.percentile(z, 5))
    hist, edges = np.histogram(z[mask], bins=80)
    threshold = float(hist.max()) * min_frac
    for i, count in enumerate(hist):
        if count >= threshold:
            return float((edges[i] + edges[i + 1]) / 2.0)
    return float(np.median(z[mask]))


def dense_occupancy(
    points: np.ndarray,
    *,
    cell_m: float = 0.2,
    inflate_m: float = 0.35,
    obs_max_z: float = 2.0,
) -> tuple[OccupancyGrid, float]:
    """稠密点云 → 占用网格。返回 (grid, floor_z)。"""
    floor_z = estimate_floor_z(points)
    grid = OccupancyGrid.build(
        points,
        cell_m=cell_m,
        floor_max_z=floor_z + 0.08,
        obs_min_z=floor_z + 0.05,
        obs_max_z=obs_max_z,
        inflate_m=inflate_m,
    )
    return grid, floor_z
