#!/usr/bin/env python3
"""Associate lifted 3D objects across frames into persistent instances.

Two matching modes:
- legacy YOLO: same class + 3D center distance (family map degenerates to
  identity, so behavior is unchanged).
- open-vocab (YOLO-World): match within a semantic *family* (e.g. bucket and
  trash can are both "container") + 3D distance, then vote the final label
  across frames so single-frame mislabels do not enter memory.

Each instance carries:
- class        : majority label (by summed per-frame confidence)
- class_candidates : top-k (label, votes, max_conf, n_frames)
- family       : semantic family used for association

Usage:
    python scripts/associate_objects.py <objects.jsonl> <instances.jsonl> \
        [--dist 0.6] [--min-observations 2] [--topk 3]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

import numpy as np


FAMILY = {
    # containers / vessels
    "cup": "container", "mug": "container", "water cup": "container",
    "bottle": "container", "vase": "container", "flower vase": "container",
    "bucket": "container", "water bucket": "container", "plastic bucket": "container",
    "flower bucket": "container", "trash can": "container", "trash bin": "container",
    "wastebin": "container", "waste basket": "container", "garbage can": "container",
    "flower pot": "container", "plant pot": "container", "planter": "container",
    "垃圾桶": "container", "水桶": "container", "花筒": "container", "杯子": "container",
    # furniture
    "chair": "furniture", "stool": "furniture", "table": "furniture",
    "desk": "furniture", "coffee table": "furniture", "sofa": "furniture",
    "couch": "furniture", "bed": "furniture", "dining table": "furniture",
    "sink": "furniture", "toilet": "furniture", "沙发": "furniture",
    "桌子": "furniture", "茶几": "furniture",
    # electronics / devices
    "laptop": "electronics", "cell phone": "electronics", "phone": "electronics",
    "smartphone": "electronics", "remote": "electronics", "remote control": "electronics",
    "mouse": "electronics", "computer mouse": "electronics", "keyboard": "electronics",
    "tv": "electronics", "television": "electronics", "clock": "electronics",
    "电视": "electronics", "遥控器": "electronics", "鼠标": "electronics",
    "键盘": "electronics", "手机": "electronics",
    # textiles
    "pillow": "textile", "cushion": "textile", "blanket": "textile",
    "quilt": "textile", "被子": "textile", "枕头": "textile",
    # plants
    "potted plant": "plant", "花盆": "plant",
    # paper
    "book": "paper", "书本": "paper",
}


def family_of(cls: str) -> str:
    return FAMILY.get(cls, cls)


def ref_center(inst: dict) -> np.ndarray:
    """Match reference: median of existing centers (>=3 obs), else last."""
    obs = inst["obs"]
    if len(obs) >= 3:
        return np.median([o[0] for o in obs], axis=0)
    return obs[-1][0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("objects", type=str)
    ap.add_argument("out", type=str)
    ap.add_argument("--dist", type=float, default=0.6)
    ap.add_argument("--family-dist", type=float, default=0.2)
    ap.add_argument("--min-observations", type=int, default=2)
    ap.add_argument("--topk", type=int, default=3)
    args = ap.parse_args()

    objs = [json.loads(line) for line in open(args.objects)]
    # observation tuple: (center, frame, class, class_id, conf, box3d, bbox2d)
    instances: list[dict] = []
    for o in objs:
        c = np.array(o["center"])
        fam = family_of(o["class"])
        best_i, best_d = None, None
        # exact-class candidate (same threshold as legacy YOLO)
        for i, inst in enumerate(instances):
            if inst["obs"][0][2] != o["class"]:
                continue
            d = float(np.linalg.norm(ref_center(inst) - c))
            if d < args.dist and (best_d is None or d < best_d):
                best_d = d
                best_i = i
        # family candidate (tight threshold): same physical object carrying
        # different labels (bucket<->trash can, blanket<->pillow). Wins only
        # when it is closer than the same-class candidate.
        if fam in ("container", "textile", "plant"):
            best_fi, best_fd = None, None
            for i, inst in enumerate(instances):
                if inst["family"] != fam:
                    continue
                d = float(np.linalg.norm(ref_center(inst) - c))
                if d < args.family_dist and (best_fd is None or d < best_fd):
                    best_fd = d
                    best_fi = i
            if best_fi is not None and (best_i is None or best_fd <= best_d):
                best_i, best_d = best_fi, best_fd
        if best_i is None:
            instances.append(
                {
                    "family": fam,
                    "obs": [
                        (c, o["frame"], o["class"], o.get("class_id", -1),
                         o["conf"], o.get("box3d"), o.get("bbox2d"))
                    ],
                }
            )
        else:
            instances[best_i]["obs"].append(
                (c, o["frame"], o["class"], o.get("class_id", -1),
                 o["conf"], o.get("box3d"), o.get("bbox2d"))
            )

    rows = []
    for i, inst in enumerate(instances):
        obs = inst["obs"]
        frames = sorted({fr for _, fr, *_ in obs})
        if len(frames) < args.min_observations:
            continue
        # per (frame, class) keep the best confidence, then sum votes per class
        by_frame_class: dict[tuple[str, str], float] = {}
        for _, fr, cls, _cid, conf, _box, _bb in obs:
            by_frame_class[(fr, cls)] = max(by_frame_class.get((fr, cls), 0.0), conf)
        votes: dict[str, list[float]] = defaultdict(list)
        for (fr, cls), conf in by_frame_class.items():
            votes[cls].append(conf)
        ranked = sorted(
            ((sum(v), max(v), len(v), cls) for cls, v in votes.items()),
            key=lambda x: (-x[0], -x[1]),
        )
        label = ranked[0][3]
        candidates = [
            {"class": cls, "votes": round(s, 3), "max_conf": round(m, 3),
             "n_frames": n}
            for s, m, n, cls in ranked[: args.topk]
        ]
        center = np.median([o[0] for o in obs], axis=0).tolist()
        # average box3d per frame, then 25th-percentile box over frames
        box_by_frame: dict[str, list[list]] = {}
        for o in obs:
            if o[5] is not None:
                box_by_frame.setdefault(o[1], []).append(o[5])
        boxes = np.array([np.mean(v, axis=0) for v in box_by_frame.values()])
        box = np.percentile(boxes, 25, axis=0).tolist()
        confs = [o[4] for o in obs]
        # representative 2D evidence: highest-confidence observation
        best_obs = max(obs, key=lambda o: o[4])
        evidence = {
            "frame": best_obs[1],
            "class": best_obs[2],
            "conf": best_obs[4],
            "bbox2d": best_obs[6],
        }
        rows.append(
            {
                "instance_id": f"{label}_{i}",
                "class": label,
                "class_id": -1,
                "family": inst["family"],
                "class_candidates": candidates,
                "center": center,
                "box3d": box,
                "n_observations": len(frames),
                "first_frame": frames[0],
                "last_frame": frames[-1],
                "median_conf": float(np.median(confs)),
                "evidence": evidence,
            }
        )

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"instances: {len(rows)} (kept >= {args.min_observations} obs) -> {args.out}")
    for r in rows:
        cands = ", ".join(f"{c['class']}({c['votes']})" for c in r["class_candidates"])
        print(
            f"  {r['instance_id']}: fam={r['family']} cands=[{cands}] "
            f"obs={r['n_observations']} conf={r['median_conf']:.2f}"
        )


if __name__ == "__main__":
    main()
