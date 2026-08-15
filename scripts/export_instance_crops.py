#!/usr/bin/env python3
"""Export a representative 2D crop per instance for cloud-VLM re-naming.

Each instance's evidence (highest-confidence frame + bbox2d) is cropped from
the frame with a small margin and saved as evidence/crops/<instance_id>.jpg.
These crops are the handoff artifacts for the later cloud-VLM analysis stage.

Usage:
    python scripts/export_instance_crops.py <frames_dir> <instances.jsonl> \
        <out_dir> [--margin 12] [--min-obs 2]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir", type=Path)
    ap.add_argument("instances", type=str)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--margin", type=int, default=12)
    ap.add_argument("--min-obs", type=int, default=2)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for line in open(args.instances):
        inst = json.loads(line)
        ev = inst.get("evidence")
        if not ev or inst["n_observations"] < args.min_obs:
            continue
        img = cv2.imread(str(args.frames_dir / ev["frame"]))
        if img is None:
            continue
        h, w = img.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in ev["bbox2d"])
        x1 = max(0, x1 - args.margin)
        y1 = max(0, y1 - args.margin)
        x2 = min(w, x2 + args.margin)
        y2 = min(h, y2 + args.margin)
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        crop = img[y1:y2, x1:x2]
        out = args.out_dir / f"{inst['instance_id']}.jpg"
        cv2.imwrite(str(out), crop)
        n += 1
        print(f"{inst['instance_id']}: {ev['frame']} {ev['class']} {ev['conf']:.2f} -> {out}")
    print(f"exported {n} crops -> {args.out_dir}")


if __name__ == "__main__":
    main()
