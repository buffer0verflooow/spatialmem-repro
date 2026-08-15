#!/usr/bin/env python3
"""Extract frames + timeline manifest from a first-person recording session.

Usage:
    python scripts/prepare_session.py <session_dir> <out_dir> [--fps 3]

Reads video.mp4 (via ffmpeg) and video_timeline.csv; writes:
    <out_dir>/frames/frame_%06d.jpg
    <out_dir>/manifest.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--fps", type=float, default=3.0)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    args = ap.parse_args()

    session = args.session_dir
    video = session / "video.mp4"
    timeline = session / "video_timeline.csv"
    if not video.exists():
        raise SystemExit(f"video not found: {video}")

    frames_dir = args.out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # extract frames at the requested rate
    subprocess.run(
        [
            args.ffmpeg,
            "-y",
            "-i",
            str(video),
            "-vf",
            f"fps={args.fps}",
            "-q:v",
            "3",
            str(frames_dir / "frame_%06d.jpg"),
        ],
        check=True,
    )

    # build manifest: one row per extracted frame, aligned to the timeline.
    # The timeline has one row per decoded video frame; the extracted set is a
    # subsample, so map extracted frame i to timeline row round(i*(N-1)/(M-1)).
    timeline_rows: list[dict] = []
    if timeline.exists():
        with timeline.open() as f:
            for row in csv.DictReader(f):
                timeline_rows.append(
                    {
                        "frame_index": int(row["frame_index"]),
                        "sender_ts_ns": int(row["sender_ts_ns"]),
                        # 兼容 m1_record（host_mono_ns）与手机 App（arrival_ns）两种时间线。
                        "host_mono_ns": int(
                            row.get("host_mono_ns") or row.get("arrival_ns") or 0
                        ),
                        "flags": int(row["flags"]),
                    }
                )

    extracted = sorted(frames_dir.glob("frame_*.jpg"))
    manifest: list[dict] = []
    n = len(timeline_rows)
    m = len(extracted)
    if n and m:
        for i in range(m):
            src = timeline_rows[round(i * (n - 1) / (m - 1))]
            manifest.append(
                {
                    "extract_index": i,
                    "frame_file": extracted[i].name,
                    **src,
                }
            )
    elif n == 0:
        for i in range(m):
            manifest.append(
                {
                    "extract_index": i,
                    "frame_file": extracted[i].name,
                }
            )

    with (args.out_dir / "manifest.jsonl").open("w") as f:
        for m in manifest:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    print(f"frames -> {frames_dir}")
    print(f"manifest rows: {len(manifest)} (extracted={m}, timeline={n})")


if __name__ == "__main__":
    main()
