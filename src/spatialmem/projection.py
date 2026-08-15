"""Project metric 3D points into COLMAP SIMPLE_RADIAL camera frames."""

from __future__ import annotations

import numpy as np


def project_points(
    pts: np.ndarray,  # (N,3) metric world coords
    pose: np.ndarray,  # 4x4 world->camera
    intrinsics: tuple[float, float, float, float],  # (f, cx, cy, k)
) -> tuple[np.ndarray, np.ndarray]:
    """Return (pixels (N,2), depths (N,)) with radial distortion applied."""
    f, cx, cy, k = intrinsics
    R, t = pose[:3, :3], pose[:3, 3]
    cam = (pts @ R.T) + t  # (N,3) camera coords
    z = cam[:, 2]
    x = cam[:, 0] / np.maximum(z, 1e-9)
    y = cam[:, 1] / np.maximum(z, 1e-9)
    r2 = x * x + y * y
    scale = 1.0 + k * r2
    u = f * x * scale + cx
    v = f * y * scale + cy
    return np.stack([u, v], axis=1), z


def project_one(pts: np.ndarray, pose: np.ndarray, intrinsics) -> tuple[np.ndarray, np.ndarray]:
    return project_points(np.asarray(pts).reshape(1, 3), pose, intrinsics)

