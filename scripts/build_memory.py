#!/usr/bin/env python3
"""Build a SpatialMemory from anchors + supports + instances and validate predicates.

装配逻辑见 spatialmem.builder，本脚本只保留 CLI 与演示输出。

Usage:
    python scripts/build_memory.py <anchors.jsonl> <instances.jsonl> \
        <metric_cloud.npz> [--supports supports.jsonl] [--near-m 1.0] [--out out.json]
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from spatialmem.builder import build_memory_from_artifacts
from spatialmem.query import locate, to_egocentric


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("anchors", type=str)
    ap.add_argument("instances", type=str)
    ap.add_argument("npz", type=str)
    ap.add_argument("--near-m", type=float, default=1.0)
    ap.add_argument("--supports", type=str, default=None)
    ap.add_argument("--on-z-tol", type=float, default=0.12)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    data = np.load(args.npz)
    anchors = [json.loads(line) for line in open(args.anchors)]
    instances = [json.loads(line) for line in open(args.instances)]
    supports = None
    if args.supports:
        supports = [json.loads(line) for line in open(args.supports)]

    mem = build_memory_from_artifacts(
        anchors=anchors,
        supports=supports,
        instances=instances,
        points_metric=data["points_metric"],
        near_m=args.near_m,
        on_z_tol=args.on_z_tol,
    )
    room = next(n for n in mem.nodes() if n.node_type == "room")
    obj_nodes = [n for n in mem.nodes() if n.node_type == "object"]
    n_relations = len(mem.active_relations())
    on_fired = [
        (
            mem.get_node(r.subject).label,
            r.object,
            round(mem.get_node(r.object).box[5], 2),
        )
        for r in mem.active_relations()
        if r.predicate == "on" and r.object.startswith("support")
    ]

    print(f"memory: nodes={len(mem.nodes())} relations={n_relations}")
    print(f"room box: {[round(v,2) for v in room.box]}")
    print(f"objects: {[n.label for n in obj_nodes]}")
    print(f"on relations fired: {on_fired}")
    print(f"relations: {[r.key() for r in mem.active_relations()][:20]}")

    # demonstration: locate + egocentric answer from the last camera pose
    viewer_pose = data["poses_metric"][-1]
    for label in sorted({n.label for n in obj_nodes}):
        hits = locate(mem, label)
        if not hits:
            continue
        node = hits[0]
        ans = to_egocentric(mem, node.node_id, viewer_pose)
        if ans:
            print(
                f"  Q: '{label}' -> {ans['label']} {ans['tag']} "
                f"{ans['distance']:.2f}m"
            )

    if args.out:
        dump = {
            "nodes": [
                {
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "label": n.label,
                    "category": n.category,
                    "box": list(n.box) if n.box else None,
                    "attributes": n.attributes,
                    "parent_id": n.parent_id,
                    "confidence": n.confidence,
                }
                for n in mem.nodes()
            ],
            "relations": [
                {
                    "subject": r.subject,
                    "predicate": r.predicate,
                    "object": r.object,
                    "confidence": r.confidence,
                    "status": r.status,
                }
                for r in mem.active_relations()
            ],
        }
        with open(args.out, "w") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        print(f"memory dumped -> {args.out}")
if __name__ == "__main__":
    main()
