#!/usr/bin/env python3
"""画质门控验证：统计帧通过率，并校验证据帧（640 通过 / 320、160 拒绝）。

Usage:
    python scripts/validate_quality_gate.py [--data-dir data/cup_walk]
"""

from __future__ import annotations

import argparse
import collections
from pathlib import Path

from PIL import Image

from spatialmem.quality import evaluate


def downscale(jpeg: bytes, max_edge: int) -> bytes:
    img = Image.open(__import__("io").BytesIO(jpeg)).convert("RGB")
    img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buf = __import__("io").BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/cup_walk")
    args = ap.parse_args()

    frames_dir = Path(args.data_dir) / "frames"
    evidence_dir = Path(args.data_dir) / "evidence"
    all_frames = sorted(frames_dir.glob("*.jpg"))

    stats: dict[str, int] = collections.Counter()
    accepted = 0
    for p in all_frames:
        q = evaluate(p.read_bytes())
        ok, reasons = q.acceptable()
        if ok:
            accepted += 1
        else:
            for r in reasons:
                stats[r] += 1
    total = len(all_frames)
    print(f"== 全量帧门控（{total} 张）==")
    print(f"通过: {accepted}/{total}（{accepted / max(1, total):.0%}）")
    print(f"拒绝原因分布: {dict(stats)}")

    print("\n== 证据帧（640 应全过）==")
    for p in sorted(evidence_dir.glob("frame_*.jpg")):
        q = evaluate(p.read_bytes())
        ok, reasons = q.acceptable()
        print(f"  {p.name}: {'✓' if ok else '✗ ' + str(reasons)} "
              f"(mean={q.mean_luma:.0f} blur_var={q.blur_variance:.0f})")

    print("\n== 降质变体（320/160 应全拒）==")
    for p in sorted(evidence_dir.glob("frame_*.jpg")):
        for edge in (320, 160):
            q = evaluate(downscale(p.read_bytes(), edge))
            ok, reasons = q.acceptable()
            expect_reject = not ok
            mark = "✓" if expect_reject else "✗ 未拒"
            print(f"  {p.name}@{edge}px: {mark} {reasons}")


if __name__ == "__main__":
    main()
