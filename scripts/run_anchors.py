#!/usr/bin/env python3
"""Extract wall anchors from the metric point cloud.

Usage:
    python scripts/run_anchors.py <metric_cloud.npz> <anchors.jsonl>
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from spatialmem.anchors import extract_walls, plane_box


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", type=str)
    ap.add_argument("out", type=str)
    args = ap.parse_args()

    data = np.load(args.npz)
    pts = data["points_metric"]
    # exclude floor band and ceiling noise
    mask = (pts[:, 2] > 0.3) & (pts[:, 2] < 3.0)
    pts = pts[mask]

    walls = extract_walls(pts)
    rows = []
    for i, w in enumerate(walls):
        box = plane_box(w.inlier_points)
        n_inliers = len(w.inlier_points)
        rows.append(
            {
                "anchor_id": f"wall_{i}",
                "category": "wall",
                "box": list(box),
                "normal": w.normal.tolist(),
                "n_inliers": n_inliers,
                "confidence": min(1.0, n_inliers / 300.0),
            }
        )
        print(f"wall_{i}: box={[round(v,2) for v in box]} n={n_inliers} normal={np.round(w.normal,3)}")

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} anchors -> {args.out}")


if __name__ == "__main__":
    main()
