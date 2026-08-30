#!/usr/bin/env python3
"""从度量点云生成可通行地图并规划到目标（门/窗/任意点）的航点。

用法：
    python scripts/plan_navigation.py <metric_cloud.npz> \
        --start-x 1.0 --start-y 0.0 --goal-x 1.4 --goal-y 1.9 \
        [--anchors anchors.jsonl] [--goal-category door] [--out plan.json]

输出：转折点 + 逐段播报文本（直行/左转/右转 + 段长）。
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from spatialmem.map2d import OccupancyGrid, simplify_waypoints, waypoint_text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("npz", type=str, help="COLMAP 度量点云 points_metric")
    ap.add_argument("--start-x", type=float, required=True)
    ap.add_argument("--start-y", type=float, required=True)
    ap.add_argument("--goal-x", type=float, default=None)
    ap.add_argument("--goal-y", type=float, default=None)
    ap.add_argument("--anchors", type=str, default=None, help="anchors.jsonl")
    ap.add_argument("--goal-category", type=str, default="door",
                    help="从 anchors 中取该类别锚点中心作为目标")
    ap.add_argument("--cell-m", type=float, default=0.2)
    ap.add_argument("--inflate-m", type=float, default=0.35)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    data = np.load(args.npz)
    points = np.asarray(data["points_metric"])
    grid = OccupancyGrid.build(points, cell_m=args.cell_m, inflate_m=args.inflate_m)

    goal = None
    if args.anchors:
        with open(args.anchors) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            if r.get("category") == args.goal_category:
                box = r["box"]
                goal = np.array([(box[0] + box[3]) / 2, (box[1] + box[4]) / 2])
                print(f"目标: {r.get('anchor_id')} 中心 {goal.tolist()}")
                break
    if goal is None:
        if args.goal_x is None or args.goal_y is None:
            raise SystemExit("请提供 --goal-x/--goal-y 或 --anchors 中的目标锚点")
        goal = np.array([args.goal_x, args.goal_y])

    start = np.array([args.start_x, args.start_y])
    path = grid.plan_path(start, goal)
    if not path:
        print("无可通行路径")
        return
    waypoints = simplify_waypoints(path)
    text = waypoint_text(waypoints, args.goal_category)
    print("转折点:")
    for p in waypoints:
        print(f"  ({p[0]:.2f}, {p[1]:.2f})")
    print(f"播报: {text}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(
                {
                    "start": start.tolist(),
                    "goal": goal.tolist(),
                    "waypoints": [p.tolist() for p in waypoints],
                    "text": text,
                    "path_len_m": float(sum(
                        np.linalg.norm(waypoints[i] - waypoints[i - 1])
                        for i in range(1, len(waypoints))
                    )),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
