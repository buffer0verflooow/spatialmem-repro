#!/usr/bin/env python3
"""Run COLMAP (feature extraction -> sequential matching -> mapping) on frames.

Usage:
    python scripts/run_colmap.py <frames_dir> <out_dir> [--colmap colmap]

Outputs COLMAP sparse reconstruction under <out_dir>/sparse/0/ plus a
poses.json with per-image world->camera poses and intrinsics.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from spatialmem.colmap_io import read_images_binary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--colmap", default="colmap")
    ap.add_argument("--matcher", default="sequential", choices=["sequential", "exhaustive"])
    args = ap.parse_args()

    colmap = args.colmap
    frames = args.frames_dir.resolve()
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    db = out / "colmap.db"
    sparse = out / "sparse"
    sparse.mkdir(parents=True, exist_ok=True)

    log = (out / "colmap_run.log").open("a")

    def run(*cmd: str) -> None:
        print(f"  running: {' '.join(cmd[:2])} ...", flush=True)
        subprocess.run(
            [colmap, *cmd],
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    if db.exists():
        db.unlink()

    print("[1/3] feature extraction")
    run(
        "feature_extractor",
        "--database_path", str(db),
        "--image_path", str(frames),
        "--ImageReader.single_camera", "1",
        "--ImageReader.camera_model", "SIMPLE_RADIAL",
    )

    print("[2/3] matching")
    if args.matcher == "sequential":
        run(
            "sequential_matcher",
            "--database_path", str(db),
            "--SequentialMatching.overlap", "12",
            "--SequentialMatching.quadratic_overlap", "1",
        )
    else:
        run("exhaustive_matcher", "--database_path", str(db))

    print("[3/3] mapping")
    run(
        "mapper",
        "--database_path", str(db),
        "--image_path", str(frames),
        "--output_path", str(sparse),
        "--Mapper.min_num_matches", "15",
        "--Mapper.ba_refine_focal_length", "1",
    )

    # 视频常因转动/运动模糊断裂成多个模型；取注册图像数最多的那个作为主模型，
    # 其余段可作为后续补建（与 cup_walk 的处理方式一致）。
    cands = [p for p in sparse.iterdir() if p.is_dir() and (p / "images.bin").exists()]
    if not cands:
        raise SystemExit("COLMAP mapper produced no model")
    best = max(
        cands,
        key=lambda p: len(read_images_binary(p / "images.bin")),
    )
    print(f"using model dir: {best}（注册图像 "
          f"{len(read_images_binary(best / 'images.bin'))} 张）")
    models = [best / n for n in ("cameras.bin", "images.bin", "points3D.bin")]

    print("done")
    log.close()


if __name__ == "__main__":
    main()
