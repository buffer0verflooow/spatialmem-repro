"""Spatial predicate computation.

Follows SpatialMem's convention: vertical predicates (on/above/below) are
evaluated once in the global aligned frame; lateral predicates (left/right/
front/behind) are egocentric and computed against a viewer pose at query time.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .geometry import (
    Box,
    box_bottom_z,
    box_center,
    box_top_z,
    euclidean,
    footprint_overlap,
    horizontal_distance,
    pose_translate,
)


def predicate_on(
    child: Box,
    support: Box,
    z_tol: float = 0.05,
    min_overlap: float = 0.15,
) -> bool:
    """Object bottom roughly level with support top AND footprints overlap."""
    z_ok = abs(box_bottom_z(child) - box_top_z(support)) <= z_tol
    return z_ok and footprint_overlap(child, support) >= min_overlap


def predicate_above_below(a: Box, b: Box, margin: float = 0.02) -> Optional[str]:
    """'above' / 'below' by center z. Returns None when roughly equal."""
    za, zb = box_center(a)[2], box_center(b)[2]
    if za - zb > margin:
        return "above"
    if zb - za > margin:
        return "below"
    return None


def predicate_near(a: Box, b: Box, threshold: float = 1.0) -> bool:
    """3D center distance within threshold."""
    return euclidean(box_center(a), box_center(b)) <= threshold


def predicate_contains(container: Box, item: Box, margin: float = 0.0) -> bool:
    """Item footprint inside container footprint (plus margin) and z within."""
    cx1, cy1, cx2, cy2 = container[0], container[1], container[3], container[4]
    ix1, iy1, ix2, iy2 = item[0], item[1], item[3], item[4]
    inside_xy = (
        ix1 >= cx1 - margin
        and iy1 >= cy1 - margin
        and ix2 <= cx2 + margin
        and iy2 <= cy2 + margin
    )
    z_ok = box_bottom_z(item) >= box_bottom_z(container) - margin and box_top_z(
        item
    ) <= box_top_z(container) + margin
    return inside_xy and z_ok


def egocentric_direction(
    target_center,
    viewer_pose: np.ndarray,
    front_cone_deg: float = 45.0,
    side_cone_deg: float = 45.0,
) -> dict:
    """Describe target from the viewer's pose frame.

    viewer_pose: 4x4 world->camera pose (camera looks along +z? We define
    camera forward as +x after transform, see pose_translate). Returns
    distance (m), bearing_deg, and a coarse tag (front/left/right/behind).
    """
    local = pose_translate(np.asarray(target_center, dtype=float), viewer_pose)
    x, y, z = float(local[0]), float(local[1]), float(local[2])
    dist = float(np.hypot(x, y))
    bearing = float(np.degrees(np.arctan2(y, x)))  # +y left, -y right in cam frame
    f = front_cone_deg
    s = side_cone_deg
    if abs(bearing) <= f:
        tag = "front"
    elif bearing > f and bearing <= f + s:
        tag = "left_front"
    elif bearing < -f and bearing >= -(f + s):
        tag = "right_front"
    elif bearing > 90 + s / 2 or bearing < -(90 + s / 2):
        tag = "behind"
    else:
        tag = "left" if bearing > 0 else "right"
    return {"distance": dist, "bearing_deg": bearing, "tag": tag, "z_rel": z}

