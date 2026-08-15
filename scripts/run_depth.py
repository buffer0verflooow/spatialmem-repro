#!/usr/bin/env python3
"""MiDaS depth maps with per-frame metric calibration against COLMAP sparse points.

Usage:
    python scripts/run_depth.py <frames_dir> <metric_cloud.npz> <out_dir> \
        [--model midas_v21_small_256.onnx] [--step 3]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from spatialmem.projection import project_points

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir", type=Path)
    ap.add_argument("npz", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--model", type=Path, default=Path("midas_v21_small_256.onnx"))
    ap.add_argument("--step", type=int, default=3)
    args = ap.parse_args()

    data = np.load(args.npz)
    pts = data["points_metric"]
    poses = data["poses_metric"]
    names = [str(n) for n in data["frame_names"]]
    f, cx, cy, k = (float(v) for v in data["intrinsics"])
    intrinsics = (f, cx, cy, k)

    sess = ort.InferenceSession(str(args.model), providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name

    depth_dir = args.out_dir / "depth"
    depth_dir.mkdir(parents=True, exist_ok=True)
    calib: list[dict] = []

    n = len(names)
    for i in range(0, n, args.step):
        name = names[i]
        img = cv2.imread(str(args.frames_dir / name))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        small = cv2.resize(rgb, (256, 256), interpolation=cv2.INTER_AREA).astype(np.float32)
        small = (small / 255.0 - MEAN) / STD
        blob = small.transpose(2, 0, 1)[None, ...]
        disp = sess.run(None, {in_name: blob})[0][0]  # (256,256) inverse depth

        # metric calibration: d_metric ~= C / disp, C = median(d_metric * disp)
        pose = poses[i]
        uv, z = project_points(pts, pose, intrinsics)
        in_view = (
            (uv[:, 0] >= 1)
            & (uv[:, 0] < 639)
            & (uv[:, 1] >= 1)
            & (uv[:, 1] < 359)
            & (z > 0.2)
        )
        px = uv[in_view][:, 0]
        py = uv[in_view][:, 1]
        z_sel = z[in_view]
        if len(px) > 20:
            pu = px / 640.0 * 255.0
            pv = py / 360.0 * 255.0
            x0 = np.floor(pu).astype(int)
            y0 = np.floor(pv).astype(int)
            x1 = np.minimum(x0 + 1, 255)
            y1 = np.minimum(y0 + 1, 255)
            wx = pu - x0
            wy = pv - y0
            disp_s = (
                disp[y0, x0] * (1 - wx) * (1 - wy)
                + disp[y0, x1] * wx * (1 - wy)
                + disp[y1, x0] * (1 - wx) * wy
                + disp[y1, x1] * wx * wy
            )
            valid = (disp_s > 1e-4) & np.isfinite(disp_s)
            if valid.sum() >= 10:
                ratios = z_sel[valid] * disp_s[valid]
                lo, hi = np.percentile(ratios, 10), np.percentile(ratios, 90)
                C = float(np.median(ratios[(ratios >= lo) & (ratios <= hi)]))
            else:
                C = 0.0
        else:
            C = 0.0
        if C <= 0:
            print(f"  warn: frame {name}: no calibration points ({len(px)})")
            C = np.nan

        # metric depth map at native resolution (float16 to save space)
        disp_up = cv2.resize(disp, (640, 360), interpolation=cv2.INTER_LINEAR)
        depth = np.where(disp_up > 1e-4, C / np.maximum(disp_up, 1e-4), 0.0).astype(np.float16)
        np.save(depth_dir / (name.replace(".jpg", ".npy")), depth)
        calib.append(
            {
                "frame": name,
                "C": C,
                "n_calib_points": int(len(px)),
                "median_disp": float(np.median(disp)),
            }
        )
        if (i // args.step) % 20 == 0:
            print(f"frame {i}/{n}: C={C:.3f}")

    with (args.out_dir / "depth_calib.jsonl").open("w") as f:
        for c in calib:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"depth maps -> {depth_dir}, calib rows: {len(calib)}")


if __name__ == "__main__":
    main()
