#!/usr/bin/env python3
"""Export COLMAP poses to pose.jsonl aligned with the frame manifest.

Usage:
    python scripts/export_poses.py <out_dir> <manifest.jsonl> <poses_out.jsonl>

Requires <out_dir>/sparse/0/{cameras,images,points3D}.bin
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from spatialmem.align import align_to_floor
from spatialmem.colmap_io import read_cameras_binary, read_images_binary, read_points3d_binary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("poses_out", type=Path)
    ap.add_argument("--height-prior", type=float, default=1.55)
    ap.add_argument("--model", type=int, default=0)
    ap.add_argument("--max-jump", type=float, default=2.0)
    args = ap.parse_args()

    sparse = args.out_dir / "sparse" / str(args.model)
    cameras = read_cameras_binary(sparse / "cameras.bin")
    images = read_images_binary(sparse / "images.bin")
    points, track = read_points3d_binary(sparse / "points3D.bin")
    print(f"images={len(images)} points={len(points)}")
    # keep well-triangulated points only (track >= 3)
    points = points[track >= 3]
    print(f"points after track>=3 filter: {len(points)}")

    by_name = {img.name: img for img in images.values()}

    manifest = [json.loads(line) for line in args.manifest.open()]
    registered = []
    for row in manifest:
        img = by_name.get(row["frame_file"])
        if img is None:
            continue
        registered.append((row, img))
    print(f"registered frames: {len(registered)}/{len(manifest)}")
    if not registered:
        raise SystemExit("no frames registered in COLMAP model")

    camera_poses = np.stack([img.pose_matrix() for _, img in registered])
    align = align_to_floor(points, camera_poses, camera_height_prior=args.height_prior)
    print(
        f"alignment: scale={align.scale:.4f} floor_inliers={align.n_floor} "
        f"floor_h_raw={align.floor_h_raw:.4f}"
    )

    rows = []
    dropped = 0
    prev = None
    for row, img in registered:
        T = align.transform_pose(img.pose_matrix())
        R, t = T[:3, :3], T[:3, 3]
        center = -R.T @ t  # camera center in the aligned metric frame
        if prev is not None and np.linalg.norm(center - prev) > args.max_jump:
            dropped += 1
            continue
        prev = center
        q = _rotmat_to_quat(R)
        rows.append(
            {
                "frame_file": row["frame_file"],
                "extract_index": row.get("extract_index"),
                "frame_index": row.get("frame_index"),
                "host_mono_ns": row.get("host_mono_ns"),
                "tx": float(t[0]),
                "ty": float(t[1]),
                "tz": float(t[2]),
                "qx": q[0],
                "qy": q[1],
                "qz": q[2],
                "qw": q[3],
                "accuracy": 1.0,
                "scale": float(align.scale),
            }
        )

    with args.poses_out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} poses (dropped {dropped} outlier frames) -> {args.poses_out}")


def _rotmat_to_quat(R: np.ndarray) -> list[float]:
    """Rotation matrix (wxyz COLMAP style) -> [x, y, z, w]."""
    m00, m01, m02 = R[0]
    m10, m11, m12 = R[1]
    m20, m21, m22 = R[2]
    tr = m00 + m11 + m22
    if tr > 0:
        s = np.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (m21 - m12) / s
        y = (m02 - m20) / s
        z = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = np.sqrt(1.0 + m00 - m11 - m22) * 2
        w = (m21 - m12) / s
        x = 0.25 * s
        y = (m01 + m10) / s
        z = (m02 + m20) / s
    elif m11 > m22:
        s = np.sqrt(1.0 + m11 - m00 - m22) * 2
        w = (m02 - m20) / s
        x = (m01 + m10) / s
        y = 0.25 * s
        z = (m12 + m21) / s
    else:
        s = np.sqrt(1.0 + m22 - m00 - m11) * 2
        w = (m10 - m01) / s
        x = (m02 + m20) / s
        y = (m12 + m21) / s
        z = 0.25 * s
    return [float(x), float(y), float(z), float(w)]


if __name__ == "__main__":
    main()
