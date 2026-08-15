#!/usr/bin/env python3
"""Lift 2D detections to metric 3D boxes using the sparse reconstruction.

For each detection, the object's 3D position is the median of metric sparse
points whose projections fall inside the bounding box.

Usage:
    python scripts/lift_objects.py <metric_cloud.npz> <detections.jsonl> \
        <objects.jsonl> [--min-pts 5]
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from spatialmem.projection import project_points


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", type=str)
    ap.add_argument("detections", type=str)
    ap.add_argument("out", type=str)
    ap.add_argument("--min-pts", type=int, default=5)
    ap.add_argument("--slab-m", type=float, default=0.6)
    args = ap.parse_args()

    data = np.load(args.npz)
    pts = data["points_metric"]
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
        if pose is None:
            continue
        x1, y1, x2, y2 = d["bbox"]
        uv, z = project_points(pts, pose, intrinsics)
        in_box = (
            (uv[:, 0] >= x1 - 3)
            & (uv[:, 0] <= x2 + 3)
            & (uv[:, 1] >= y1 - 3)
            & (uv[:, 1] <= y2 + 3)
            & (z > 0.1)
        )
        z_sel = z[in_box]
        if len(z_sel) == 0:
            skipped += 1
            continue
        # foreground slab: keep points near the closest surface inside the box
        z_min = z_sel.min()
        slab = in_box & (z <= z_min + args.slab_m)
        sel = pts[slab]
        if len(sel) < args.min_pts:
            skipped += 1
            continue
        center = np.median(sel, axis=0)
        lo = np.percentile(sel, 10, axis=0)
        hi = np.percentile(sel, 90, axis=0)
        rows.append(
            {
                "frame": d["frame"],
                "class": d["class"],
                "class_id": d["class_id"],
                "conf": d["conf"],
                "bbox2d": d["bbox"],
                "center": center.tolist(),
                "box3d": [
                    float(lo[0]),
                    float(lo[1]),
                    float(lo[2]),
                    float(hi[0]),
                    float(hi[1]),
                    float(hi[2]),
                ],
                "n_points": int(len(sel)),
            }
        )

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"lifted {len(rows)} objects (skipped {skipped}) -> {args.out}")


if __name__ == "__main__":
    main()
