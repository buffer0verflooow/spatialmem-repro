#!/usr/bin/env python3
"""Export metric point cloud + aligned poses for a COLMAP model.

Usage:
    python scripts/extract_metric_cloud.py <out_dir> <npz_out> \
        --model 1 --height-prior 1.55
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
    ap.add_argument("npz_out", type=Path)
    ap.add_argument("--model", type=int, default=0)
    ap.add_argument("--height-prior", type=float, default=1.55)
    ap.add_argument("--min-track", type=int, default=3)
    args = ap.parse_args()

    sparse = args.out_dir / "sparse" / str(args.model)
    cameras = read_cameras_binary(sparse / "cameras.bin")
    images = read_images_binary(sparse / "images.bin")
    points, track = read_points3d_binary(sparse / "points3D.bin")
    mask = track >= args.min_track
    points = points[mask]

    by_name = {img.name: img for img in images.values()}
    names = sorted(by_name)
    poses = np.stack([by_name[n].pose_matrix() for n in names])
    align = align_to_floor(points, poses, camera_height_prior=args.height_prior)

    points_metric = align.transform_points(points)
    poses_metric = np.stack([align.transform_pose(p) for p in poses])
    cam = next(iter(cameras.values()))
    intrinsics = tuple(float(v) for v in cam.params[:4])

    with args.npz_out.open("wb") as f:
        np.savez(
            f,
            points_metric=points_metric,
            poses_metric=poses_metric,
            frame_names=np.array(names),
            intrinsics=np.array(intrinsics),
            scale=align.scale,
            floor_h_raw=align.floor_h_raw,
        )
    print(
        f"metric cloud: {len(points_metric)} points, {len(names)} frames -> {args.npz_out}"
    )
    print(f"intrinsics: {intrinsics}  scale: {align.scale:.4f}")


if __name__ == "__main__":
    main()

