#!/usr/bin/env python3
"""Lift 2D detections to metric 3D boxes using MiDaS depth + camera geometry.

Usage:
    python scripts/lift_objects_depth.py <metric_cloud.npz> <depth_dir> \
        <detections.jsonl> <objects.jsonl>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def back_project(u: float, v: float, d: float, pose: np.ndarray, intrinsics) -> np.ndarray:
    f, cx, cy, k = intrinsics
    x_d = (u - cx) / f
    y_d = (v - cy) / f
    r2 = x_d * x_d + y_d * y_d
    s = 1.0 + k * r2
    x_u, y_u = x_d / s, y_d / s
    p_cam = np.array([x_u * d, y_u * d, d])
    R, t = pose[:3, :3], pose[:3, 3]
    return R.T @ (p_cam - t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", type=str)
    ap.add_argument("depth_dir", type=Path)
    ap.add_argument("detections", type=str)
    ap.add_argument("out", type=str)
    ap.add_argument("--min-depth", type=float, default=0.15)
    args = ap.parse_args()

    data = np.load(args.npz)
    poses = data["poses_metric"]
    names = [str(n) for n in data["frame_names"]]
    f, cx, cy, k = (float(v) for v in data["intrinsics"])
    intrinsics = (f, cx, cy, k)
    pose_by_name = {name: poses[i] for i, name in enumerate(names)}

    dets = [json.loads(line) for line in open(args.detections)]
    rows = []
    skipped = 0
    for d in dets:
        pose = pose_by_name.get(d["frame"])
        depth_path = args.depth_dir / (d["frame"].replace(".jpg", ".npy"))
        if pose is None or not depth_path.exists():
            continue
        depth = np.load(depth_path).astype(np.float32)
        x1, y1, x2, y2 = d["bbox"]
        cu, cv_ = (x1 + x2) / 2, (y1 + y2) / 2
        # central 45% region depth
        roi = depth[
            int(y1 + 0.275 * (y2 - y1)) : int(y1 + 0.725 * (y2 - y1)),
            int(x1 + 0.275 * (x2 - x1)) : int(x1 + 0.725 * (x2 - x1)),
        ]
        vals = roi[(roi > args.min_depth) & np.isfinite(roi)]
        if len(vals) < 5:
            skipped += 1
            continue
        d_obj = float(np.median(vals))
        center = back_project(cu, cv_, d_obj, pose, intrinsics)
        w_world = (x2 - x1) / f * d_obj
        h_world = (y2 - y1) / f * d_obj
        thick = float(np.clip(0.15 * max(w_world, h_world), 0.05, 0.6))
        half = np.array([w_world / 2, max(h_world / 2, 0.1), thick / 2])
        lo = center - half
        hi = center + half
        rows.append(
            {
                "frame": d["frame"],
                "class": d["class"],
                "class_id": d["class_id"],
                "conf": d["conf"],
                "bbox2d": d["bbox"],
                "center": center.tolist(),
                "depth_m": d_obj,
                "box3d": [
                    float(lo[0]),
                    float(lo[1]),
                    float(lo[2]),
                    float(hi[0]),
                    float(hi[1]),
                    float(hi[2]),
                ],
            }
        )

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"lifted {len(rows)} objects (skipped {skipped}) -> {args.out}")


if __name__ == "__main__":
    main()

