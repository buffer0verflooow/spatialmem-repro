#!/usr/bin/env python3
"""P0-1b：用深度×位姿构建稠密度量地图并验证可通行性。

用法：
    python scripts/build_dense_map.py <data_dir> [--pixel-step 2] [--max-depth 6]

data_dir 需包含：
- metric_cloud.npz：poses_metric / frame_names / intrinsics（COLMAP 米制位姿）
- depth/*.npy：与帧同名的米制深度图（run_depth.py 输出）

输出：稠密点云 + 占用网格统计 + 全图最远两格的可通行路径验证。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from spatialmem.dense_map import dense_occupancy, fuse_dense_cloud
from spatialmem.map2d import simplify_waypoints, waypoint_text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--pixel-step", type=int, default=2)
    ap.add_argument("--max-depth", type=float, default=6.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    data = np.load(args.data_dir / "metric_cloud.npz")
    poses = data["poses_metric"]
    names = [str(n) for n in data["frame_names"]]
    f, cx, cy, k = (float(v) for v in data["intrinsics"])
    intrinsics = (f, cx, cy, k)

    depth_dir = args.data_dir / "depth"
    depths: list[np.ndarray] = []
    for name in names:
        np_path = depth_dir / name.replace(".jpg", ".npy")
        if np_path.exists():
            depths.append(np.load(np_path))
    if not depths:
        raise SystemExit(f"{depth_dir} 无深度图，请先跑 scripts/run_depth.py")
    print(f"融合 {len(depths)}/{len(names)} 帧深度×位姿…")

    dense = fuse_dense_cloud(
        poses[: len(depths)],
        intrinsics,
        depths,
        pixel_step=args.pixel_step,
        max_depth=args.max_depth,
    )
    print(f"稠密点云: {len(dense)} 点")
    if len(dense) < 1000:
        raise SystemExit("稠密点云过少，检查深度图/位姿对齐")

    grid, floor_z = dense_occupancy(dense)
    free = int((~grid.occ).sum())
    floor_cells = int(grid.floor_evidence.sum())
    print(f"地板高度: {floor_z:.2f} m")
    print(
        f"占用网格: {grid.shape[0]}x{grid.shape[1]}，"
        f"可通行 {free} 格，地板证据 {floor_cells} 格，障碍 {int(grid.occ.sum())} 格"
    )

    # 最大连通分量内做路径验证（最远两格可能分属不同孤岛，无意义）
    free_cells = np.argwhere(~grid.occ)
    if len(free_cells) < 2:
        raise SystemExit("可通行格不足，无法验证路径")
    from collections import deque

    occ = grid.occ
    nx, ny = occ.shape
    seen = np.zeros_like(occ)
    comps: list[list[np.ndarray]] = []
    for cell in free_cells:
        x, y = int(cell[0]), int(cell[1])
        if seen[x, y]:
            continue
        q = deque([(x, y)])
        seen[x, y] = True
        comp: list[np.ndarray] = []
        while q:
            cx, cy = q.popleft()
            comp.append(np.array([cx, cy]))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxp, nyp = cx + dx, cy + dy
                if 0 <= nxp < nx and 0 <= nyp < ny and not occ[nxp, nyp] and not seen[nxp, nyp]:
                    seen[nxp, nyp] = True
                    q.append((nxp, nyp))
        comps.append(comp)
    largest = max(comps, key=len)
    # 在最大分量内做两次 BFS 找"最长对"（图的直径端点）
    def bfs_farthest(src: tuple[int, int]) -> tuple[tuple[int, int], int]:
        dist = {src: 0}
        q = deque([src])
        far = src
        while q:
            cur = q.popleft()
            if dist[cur] > dist[far]:
                far = cur
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (cur[0] + dx, cur[1] + dy)
                if (
                    0 <= n[0] < nx and 0 <= n[1] < ny
                    and not occ[n] and n not in dist
                ):
                    dist[n] = dist[cur] + 1
                    q.append(n)
        return far, dist[far]

    a = tuple(int(v) for v in largest[0])
    b, _ = bfs_farthest(a)
    a2, _ = bfs_farthest(b)
    start = grid.center_of(*b)
    goal = grid.center_of(*a2)
    path = grid.plan_path(start, goal)
    if path:
        wp = simplify_waypoints(path)
        print(f"路径验证(最大连通分量 {len(largest)} 格): "
              f"({start[0]:.1f},{start[1]:.1f}) -> ({goal[0]:.1f},{goal[1]:.1f})")
        print(f"转折点 {len(wp)} 个: {[np.round(p, 2).tolist() for p in wp]}")
        print("播报: " + waypoint_text(wp, "目标"))
    else:
        print("路径验证: 最大连通分量内仍无路径（异常）")

    if args.out:
        np.savez(
            args.out,
            dense_points=dense,
            floor_z=floor_z,
            grid_shape=np.array(grid.shape),
            grid_origin=grid.origin,
            grid_occ=grid.occ,
            grid_floor=grid.floor_evidence,
            cell_m=grid.cell_m,
        )
        print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
