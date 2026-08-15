#!/usr/bin/env python3
"""M5.3 检索优先问答：确认记忆 → 先查后答（离线验证）。

对 M5.2 确认节点跑一组 QA，验证：
  - 记忆命中：「电风扇在哪」→ 根据记忆回答（含方向/距离，若锚点+位姿可用）；
  - 颜色检索：「白色」→ 列出记忆中所有白色物体；
  - 画面兜底：记忆未命中 → 标注 fallback（离线只标记，不真正扫画面）。

Usage:
    python scripts/run_m5_retrieval.py [--data-dir data/new_scene] [--out m5_retrieval.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from spatialmem.descriptions import pose_from_quat
from spatialmem.retrieval import ConfirmedMemory


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/new_scene")
    ap.add_argument("--out", default="data/new_scene/m5_retrieval.json")
    ap.add_argument("--poses", default="poses_model3.jsonl")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    confirmed = json.loads(
        (data_dir / "confirmed_nodes.json").read_text(encoding="utf-8")
    )["confirmed_nodes"]
    memory = ConfirmedMemory.from_json(confirmed)

    # 视角位姿：取注册位姿最后一帧（同 M4 自我中心评测口径）
    viewer_pose = None
    poses_path = data_dir / args.poses
    if poses_path.exists():
        rows = [json.loads(l) for l in open(poses_path)]
        if rows:
            r = rows[-1]
            viewer_pose = pose_from_quat(
                r["qx"], r["qy"], r["qz"], r["qw"], r["tx"], r["ty"], r["tz"]
            )

    queries = [
        "电风扇在哪",
        "白色电风扇什么颜色",
        "键盘在哪",
        "鼠标在哪",
        "葡萄在哪",
        "杯子在哪",
        "床上有什么",
        "不存在的蓝色恐龙在哪",
    ]

    rows = []
    print("== M5.3 检索优先问答 ==")
    for q in queries:
        ans = memory.query(q, viewer_pose=viewer_pose)
        rows.append(
            {
                "query": q,
                "found": ans.found,
                "text": ans.text,
                "fallback_used": ans.fallback_used,
                "matches": ans.matches,
            }
        )
        tag = "记忆" if ans.found else "兜底"
        print(f"[{tag}] {q}\n      {ans.text}")

    report = {
        "data_dir": str(data_dir),
        "memory_size": len(memory.nodes()),
        "viewer_pose": "last_registered" if viewer_pose is not None else None,
        "queries": rows,
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"[M5.3] -> {args.out}")


if __name__ == "__main__":
    main()
