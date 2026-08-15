"""Metric upright alignment for egocentric walking video.

Strategy (robust for glasses video):
1. Gravity direction = average camera "down" axis in the world frame
   (COLMAP cameras use x-right, y-down, z-forward, so world_down =
   R_cam^T @ [0,1,0]).
2. The floor is the densest horizontal point cluster below the lowest
   camera center, found by histogram peak.
3. Metric scale comes from the camera-height prior: cameras sit at ~1.55 m
   above the floor (SpatialMem's height-prior scale recovery).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Alignment:
    R_align: np.ndarray  # (3,3) raw world -> upright metric frame
    origin: np.ndarray  # (3,) raw-world point on the floor plane
    scale: float  # raw units -> meters
    floor_h_raw: float
    n_floor: int

    def transform_points(self, pts: np.ndarray) -> np.ndarray:
        return self.scale * ((pts - self.origin) @ self.R_align.T)

    def transform_pose(self, pose: np.ndarray) -> np.ndarray:
        """Transform a 4x4 world->camera pose into the aligned metric frame."""
        R, t = pose[:3, :3], pose[:3, 3]
        R_new = R @ self.R_align.T
        c_raw = -R.T @ t
        c_new = self.transform_points(c_raw.reshape(1, 3)).reshape(3)
        t_new = -R_new @ c_new
        out = np.eye(4)
        out[:3, :3] = R_new
        out[:3, 3] = t_new
        return out


def estimate_gravity(camera_poses: np.ndarray) -> np.ndarray:
    """Gravity from trajectory geometry: least-variance direction of camera
    centers (walking is roughly planar, so vertical spread is smallest).
    The camera down-axis average is used only to disambiguate the sign."""
    centers = np.stack([-T[:3, :3].T @ T[:3, 3] for T in camera_poses])
    c = centers - centers.mean(axis=0)
    S = c.T @ c / len(centers)
    w, V = np.linalg.eigh(S)
    g = V[:, int(np.argmin(w))]
    if not np.isfinite(g).all():
        raise ValueError("gravity estimation failed")
    # sign: align with average camera down-axis
    downs = np.stack([T[:3, :3].T @ np.array([0.0, 1.0, 0.0]) for T in camera_poses])
    g_init = downs.mean(axis=0)
    if g @ g_init < 0:
        g = -g
    return g


def align_to_floor(
    pts: np.ndarray,
    camera_poses: np.ndarray,
    camera_height_prior: float = 1.55,
) -> Alignment:
    """Align raw reconstruction to upright metric frame with z=0 at the floor."""
    with np.errstate(all="ignore"):
        g = estimate_gravity(camera_poses)

        # horizontal axes
        ref = np.array([1.0, 0.0, 0.0])
        if abs(ref @ g) > 0.9:
            ref = np.array([0.0, 1.0, 0.0])
        u = np.cross(g, ref)
        u /= np.linalg.norm(u)
        v = np.cross(g, u)
        R_align = np.stack([u, v, g])

        # project points and camera centers onto gravity axis
        h_pts = pts @ g
        centers = np.stack([-T[:3, :3].T @ T[:3, 3] for T in camera_poses])
        h_cam = centers @ g
        cam_min_h = float(h_cam.min())
        cam_med_h = float(np.median(h_cam))

        floor_h, n_floor = _find_floor(h_pts, cam_min_h)

        med_cam_above = cam_med_h - floor_h
        if med_cam_above <= 0.05:
            raise ValueError("camera centers not above estimated floor")
        scale = camera_height_prior / med_cam_above
        if not (0.001 < scale < 1000.0):
            raise ValueError(f"implausible scale {scale:.4f}")

        # origin: a raw point on the floor plane (nearest to floor height)
        origin = pts[int(np.argmin(np.abs(h_pts - floor_h)))]
        return Alignment(R_align, origin, scale, float(floor_h), n_floor)


def _find_floor(h_pts: np.ndarray, cam_min_h: float) -> tuple[float, int]:
    below = h_pts[h_pts < cam_min_h - 1e-6]
    if len(below) < 50:
        raise ValueError("too few points below camera level for floor estimate")
    lo, hi = float(below.min()), float(below.max())
    hist, edges = np.histogram(below, bins=200, range=(lo, hi))
    mid = (lo + hi) / 2
    usable = int(np.searchsorted(edges, mid, side="right")) - 1
    if usable <= 0:
        usable = len(hist) - 1
    band = hist[: usable + 1]
    if band.max() == 0:
        raise ValueError("no floor cluster found")
    peak = int(np.argmax(band))
    floor_h = (edges[peak] + edges[peak + 1]) / 2
    return floor_h, int(band[peak])
