#!/usr/bin/env python3
"""Sanity checks on the M1 pose output (pose.jsonl).

Usage:
    python scripts/validate_m1.py <poses.jsonl>

Reports trajectory continuity, walking-speed sanity, and per-axis extent.
"""

from __future__ import annotations

import argparse
import json

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("poses", type=argparse.FileType())
    args = ap.parse_args()

    rows = [json.loads(line) for line in args.poses]
    if not rows:
        raise SystemExit("empty poses file")

    pos = np.array([_camera_center(r) for r in rows])
    qw = np.array([r["qw"] for r in rows])
    t_host = [r.get("host_mono_ns") for r in rows]

    # trajectory continuity: frame-to-frame translation norm
    d = np.linalg.norm(np.diff(pos, axis=0), axis=1)
    total_len = float(np.sum(d))

    # inter-frame time from host_mono_ns when available
    if t_host[0] is not None and len(t_host) > 1:
        dt_sec = [
            (b - a) / 1e9 for a, b in zip(t_host[:-1], t_host[1:]) if a is not None and b is not None
        ]
        dt = float(np.median(dt_sec)) if dt_sec else 1.0 / 15.0
    else:
        dt = 1.0 / 15.0
    speeds = d / dt
    median_speed = float(np.median(speeds))

    quat = np.array([[r["qw"], r["qx"], r["qy"], r["qz"]] for r in rows])
    quat_ok = int(np.all(np.abs(np.linalg.norm(quat, axis=1) - 1.0) < 1e-3))

    print(f"frames: {len(rows)}")
    print(f"trajectory length: {total_len:.2f} m")
    print(f"median inter-frame speed: {median_speed:.2f} m/s")
    print(f"position extent (x/y/z): {pos.max(axis=0) - pos.min(axis=0)}")
    print(f"max single jump: {float(d.max()):.3f} m")
    print(f"quaternion normalized: {quat_ok}")
    print(f"timestamps present: {sum(1 for t in t_host if t is not None)}/{len(t_host)}")


def _camera_center(r: dict) -> list[float]:
    """Camera center in world frame: c = -R^T t (from quaternion + tvec)."""
    qw, qx, qy, qz = r["qw"], r["qx"], r["qy"], r["qz"]
    R = np.array(
        [
            [1 - 2 * (qy**2 + qz**2), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
            [2 * (qx * qy + qz * qw), 1 - 2 * (qx**2 + qz**2), 2 * (qy * qz - qx * qw)],
            [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx**2 + qy**2)],
        ]
    )
    t = np.array([r["tx"], r["ty"], r["tz"]])
    return (-R.T @ t).tolist()


if __name__ == "__main__":
    main()
