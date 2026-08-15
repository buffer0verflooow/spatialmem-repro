#!/usr/bin/env python3
"""Extract horizontal support surfaces (tabletops/counters) from the metric cloud.

Usage:
    python scripts/run_supports.py <metric_cloud.npz> <supports.jsonl>
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from spatialmem.anchors import fit_horizontal_planes, plane_box


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", type=str)
    ap.add_argument("out", type=str)
    ap.add_argument("--min-z", type=float, default=0.2)
    ap.add_argument("--max-z", type=float, default=2.2)
    args = ap.parse_args()

    data = np.load(args.npz)
    pts = data["points_metric"]
    mask = (pts[:, 2] >= args.min_z) & (pts[:, 2] <= args.max_z)
    pts = pts[mask]

    planes = fit_horizontal_planes(pts, min_inliers=40, inlier_eps=0.05)
    rows = []
    for i, p in enumerate(planes):
        xyz = p.inlier_points
        top_z = float(np.median(xyz[:, 2]))
        box = list(plane_box(xyz))
        # a support surface is a thin slab at its top height
        box[2] = top_z - 0.05
        box[5] = top_z + 0.03
        rows.append(
            {
                "support_id": f"support_{i}",
                "category": "support_surface",
                "top_z": top_z,
                "box": box,
                "n_inliers": int(len(xyz)),
                "extent_xy": [
                    round(float(xyz[:, 0].max() - xyz[:, 0].min()), 2),
                    round(float(xyz[:, 1].max() - xyz[:, 1].min()), 2),
                ],
            }
        )
        print(
            f"support_{i}: top_z={top_z:.2f} n={len(xyz)} "
            f"extent_xy={rows[-1]['extent_xy']}"
        )

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} supports -> {args.out}")


if __name__ == "__main__":
    main()

