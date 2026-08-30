"""3D box and pose utilities in the upright metric frame (z up)."""

from __future__ import annotations

import numpy as np

Box = tuple[float, float, float, float, float, float]


def box_center(box: Box) -> tuple[float, float, float]:
    xmin, ymin, zmin, xmax, ymax, zmax = box
    return ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)


def box_bottom_z(box: Box) -> float:
    return box[2]


def box_top_z(box: Box) -> float:
    return box[5]


def footprint(box: Box) -> tuple[float, float, float, float]:
    xmin, ymin, _, xmax, ymax, _ = box
    return (xmin, ymin, xmax, ymax)


def footprint_overlap(a: Box, b: Box) -> float:
    """Intersection area / min(footprint areas) (2D, xy footprint).

    Using the minimum area keeps small-object-on-large-support pairs
    (a cup on a table) from being penalized by IoU's denominator.
    """
    ax1, ay1, ax2, ay2 = footprint(a)
    bx1, by1, bx2, by2 = footprint(b)
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    area_a = max(1e-9, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1e-9, (bx2 - bx1) * (by2 - by1))
    return inter / min(area_a, area_b)


def euclidean(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def horizontal_distance(a: np.ndarray, b: np.ndarray) -> float:
    diff = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    return float(np.hypot(diff[0], diff[1]))


def ray_box_intersect(origin: np.ndarray, direction: np.ndarray, box: Box) -> bool:
    """Slab test: does the ray hit the axis-aligned box?"""
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    lo = np.array([box[0], box[1], box[2]])
    hi = np.array([box[3], box[4], box[5]])
    tmin = 0.0
    tmax = np.inf
    for i in range(3):
        if abs(d[i]) < 1e-12:
            if o[i] < lo[i] or o[i] > hi[i]:
                return False
            continue
        t1 = (lo[i] - o[i]) / d[i]
        t2 = (hi[i] - o[i]) / d[i]
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return False
    return True


def ray_box_interval(
    origin: np.ndarray, direction: np.ndarray, box: Box
) -> tuple | None:
    """Slab test returning (t_enter, t_exit), or None when the ray misses.

    `direction` is not required to be normalized; returned `t` uses the same
    units as `direction` (multiply by |direction| for world distance).
    """
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    lo = np.array([box[0], box[1], box[2]])
    hi = np.array([box[3], box[4], box[5]])
    tmin = 0.0
    tmax = np.inf
    for i in range(3):
        if abs(d[i]) < 1e-12:
            if o[i] < lo[i] or o[i] > hi[i]:
                return None
            continue
        t1 = (lo[i] - o[i]) / d[i]
        t2 = (hi[i] - o[i]) / d[i]
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return None
    return (tmin, tmax)


def yaw_from_pose(pose: np.ndarray) -> float:
    """Extract heading yaw (radians) from a 4x4 camera pose (z up)."""
    R = np.asarray(pose[:3, :3], dtype=float)
    return float(np.arctan2(R[1, 0], R[0, 0]))


def pose_translate(point: np.ndarray, pose: np.ndarray) -> np.ndarray:
    """Transform a 3D point from world frame into the pose frame."""
    p = np.asarray(point, dtype=float)
    T = np.asarray(pose, dtype=float)
    if T.shape == (3, 4):
        R, t = T[:, :3], T[:, 3]
    else:
        R, t = T[:3, :3], T[:3, 3]
    return R.T @ (p - t)
