"""Structural anchor extraction: walls as dominant vertical planes.

Floor plane is z=0 by construction of the metric alignment. Walls are found
by iterative RANSAC on near-vertical planes in the metric point cloud.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Plane:
    normal: np.ndarray
    point: np.ndarray
    inlier_points: np.ndarray


def fit_vertical_plane(
    pts: np.ndarray,
    ransac_iters: int = 500,
    inlier_eps: float = 0.08,
    min_inliers: int = 50,
    max_tilt_deg: float = 20.0,
    rng: np.random.Generator | None = None,
) -> tuple[Plane | None, np.ndarray | None]:
    """RANSAC for one vertical plane; returns (Plane, inlier indices)."""
    rng = rng or np.random.default_rng(1)
    n = len(pts)
    z_axis = np.array([0.0, 0.0, 1.0])
    best: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    with np.errstate(all="ignore"):
        for _ in range(ransac_iters):
            idx = rng.choice(n, size=3, replace=False)
            p = pts[idx]
            normal = np.cross(p[1] - p[0], p[2] - p[0])
            norm = np.linalg.norm(normal)
            if not np.isfinite(norm) or norm < 1e-9:
                continue
            normal /= norm
            if abs(normal @ z_axis) > np.sin(np.radians(max_tilt_deg)):
                continue  # not vertical enough
            if normal[2] < 0:
                normal = -normal
            d = -(normal @ p[0])
            dists = np.abs(pts @ normal + d)
            inliers = dists <= inlier_eps
            k = int(inliers.sum())
            if k < min_inliers:
                continue
            if best is None or k > best[2].sum():
                best = (normal, p[0], np.where(inliers)[0])
    if best is None:
        return None, None
    normal, point, idx = best
    return Plane(normal, point, pts[idx]), idx


def extract_walls(
    pts: np.ndarray,
    max_walls: int = 6,
    min_inliers: int = 50,
    inlier_eps: float = 0.08,
) -> list[Plane]:
    """Iteratively peel off dominant vertical planes (walls)."""
    remaining = pts
    walls: list[Plane] = []
    rng = np.random.default_rng(2)
    for _ in range(max_walls):
        plane, idx = fit_vertical_plane(
            remaining,
            inlier_eps=inlier_eps,
            min_inliers=min_inliers,
            rng=rng,
        )
        if plane is None:
            break
        walls.append(plane)
        keep = np.ones(len(remaining), dtype=bool)
        keep[idx] = False
        remaining = remaining[keep]
    return walls


def fit_horizontal_planes(
    pts: np.ndarray,
    max_planes: int = 8,
    min_inliers: int = 50,
    inlier_eps: float = 0.05,
    max_tilt_deg: float = 15.0,
) -> list[Plane]:
    """Iteratively peel dominant near-horizontal planes (floor + tabletops)."""
    remaining = pts
    planes: list[Plane] = []
    rng = np.random.default_rng(3)
    z_axis = np.array([0.0, 0.0, 1.0])
    for _ in range(max_planes):
        best = None
        with np.errstate(all="ignore"):
            for _ in range(400):
                idx = rng.choice(len(remaining), size=3, replace=False)
                p = remaining[idx]
                normal = np.cross(p[1] - p[0], p[2] - p[0])
                norm = np.linalg.norm(normal)
                if not np.isfinite(norm) or norm < 1e-9:
                    continue
                normal /= norm
                if normal[2] < 0:
                    normal = -normal
                if abs(normal @ z_axis) < np.cos(np.radians(max_tilt_deg)):
                    continue  # not horizontal enough
                d = -(normal @ p[0])
                dists = np.abs(remaining @ normal + d)
                inliers = dists <= inlier_eps
                k = int(inliers.sum())
                if k < min_inliers:
                    continue
                if best is None or k > best[2].sum():
                    best = (normal, p[0], np.where(inliers)[0])
        if best is None:
            break
        normal, point, idx = best
        planes.append(Plane(normal, point, remaining[idx]))
        keep = np.ones(len(remaining), dtype=bool)
        keep[idx] = False
        remaining = remaining[keep]
    return planes


def plane_box(inlier_xyz: np.ndarray, min_z: float = 0.0) -> tuple:
    """Axis-aligned metric box of the inlier cluster (heuristic)."""
    xyz = inlier_xyz
    xmin, ymin, zmin = xyz.min(axis=0)
    xmax, ymax, zmax = xyz.max(axis=0)
    return (float(xmin), float(ymin), max(min_z, float(zmin)), float(xmax), float(ymax), float(zmax))
