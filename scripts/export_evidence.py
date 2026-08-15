#!/usr/bin/env python3
"""Draw detection boxes + depth on a few frames for human verification.

Usage:
    python scripts/export_evidence.py <frames_dir> <detections.jsonl> \
        <out_dir> [--max-frames 6]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir", type=Path)
    ap.add_argument("detections", type=str)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--max-frames", type=int, default=6)
    args = ap.parse_args()

    dets = [json.loads(line) for line in open(args.detections)]
    by_frame: dict[str, list[dict]] = {}
    for d in dets:
        by_frame.setdefault(d["frame"], []).append(d)
    frames = sorted(by_frame, key=lambda f: -len(by_frame[f]))[: args.max_frames]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name in frames:
        img = cv2.imread(str(args.frames_dir / name))
        if img is None:
            continue
        for d in by_frame[name]:
            x1, y1, x2, y2 = (int(v) for v in d["bbox"])
            color = (0, 200, 0) if d["conf"] >= 0.5 else (0, 160, 255)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                img,
                f"{d['class']} {d['conf']:.2f}",
                (x1, max(12, y1 - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
        out = args.out_dir / name
        cv2.imwrite(str(out), img)
        print(f"{name}: {len(by_frame[name])} boxes -> {out}")


if __name__ == "__main__":
    main()

