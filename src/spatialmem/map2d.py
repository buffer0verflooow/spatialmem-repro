"""2D 可通行地图 + 障碍感知路径规划（P0 度量导航的基础）。

输入：度量点云（points_metric，米制，z 竖直朝上）——
- 地板带内的点 → 可通行证据；
- 0.05~2.0m 高度带内的点 → 障碍（桌/椅/讲台/墙下沿）；
- 天花板与高于伸手可及的点忽略。

输出：
- OccupancyGrid：2D 网格（True=障碍，含膨胀安全余量）；
- plan_path()：A* 8 邻域，避障 + 尽量走已验证地板；
- simplify_waypoints()：只保留转折点（直行段合并）；
- waypoint_text()：逐段转向播报（直行/左转/右转 + 段长）。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np


@dataclass
class OccupancyGrid:
    cell_m: float
    origin: np.ndarray  # (x0, y0)，网格左下角
    shape: tuple[int, int]  # (nx, ny)
    occ: np.ndarray  # bool (nx, ny)，True=不可通行（已含膨胀）
    floor_evidence: np.ndarray  # bool (nx, ny)，True=该格有地板证据

    def cell_of(self, xy: np.ndarray) -> tuple[int, int]:
        """米制坐标 → 网格坐标（取所在格，floor；越界钳制到边界内）。

        不能用 round：round(奇数格中心 .5) 会被银行家舍入到相邻格，
        造成 center_of(cell) → cell_of 不自洽（起点/终点错位导致 A* 无路）。
        """
        ix = int(np.floor((xy[0] - self.origin[0]) / self.cell_m))
        iy = int(np.floor((xy[1] - self.origin[1]) / self.cell_m))
        ix = min(max(ix, 0), self.shape[0] - 1)
        iy = min(max(iy, 0), self.shape[1] - 1)
        return ix, iy

    def center_of(self, ix: int, iy: int) -> np.ndarray:
        return self.origin + np.array(
            [(ix + 0.5) * self.cell_m, (iy + 0.5) * self.cell_m]
        )

    def nearest_free(self, xy: np.ndarray) -> np.ndarray:
        """把目标钳制/移动到最近的可通行格（起点终点可能落在障碍格）。"""
        start = self.cell_of(xy)
        if not self.occ[start]:
            return self.center_of(*start)
        # BFS 找最近 free 格
        from collections import deque

        q = deque([start])
        seen = {start}
        while q:
            c = q.popleft()
            if not self.occ[c]:
                return self.center_of(*c)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + dx, c[1] + dy)
                if 0 <= n[0] < self.shape[0] and 0 <= n[1] < self.shape[1] and n not in seen:
                    seen.add(n)
                    q.append(n)
        raise ValueError("整张地图无可通行格")

    @staticmethod
    def build(
        points_metric: np.ndarray,
        *,
        cell_m: float = 0.2,
        floor_max_z: float = 0.08,
        obs_min_z: float = 0.05,
        obs_max_z: float = 2.0,
        inflate_m: float = 0.35,
    ) -> OccupancyGrid:
        """从度量点云构建可通行网格（含障碍膨胀）。"""
        pts = np.asarray(points_metric, dtype=float)
        if pts.ndim != 2 or pts.shape[1] < 3:
            raise ValueError("points_metric 需要 (N,3)")
        xy = pts[:, :2]
        z = pts[:, 2]
        x0, y0 = np.floor(xy.min(axis=0) / cell_m) * cell_m
        x1, y1 = np.ceil(xy.max(axis=0) / cell_m) * cell_m
        nx = max(1, round((x1 - x0) / cell_m))
        ny = max(1, round((y1 - y0) / cell_m))
        occ = np.zeros((nx, ny), dtype=bool)
        floor_evidence = np.zeros((nx, ny), dtype=bool)

        floor_mask = z <= floor_max_z
        obs_mask = (z >= obs_min_z) & (z <= obs_max_z)
        if floor_mask.any():
            fxy = xy[floor_mask]
            fx = ((fxy[:, 0] - x0) / cell_m).astype(int)
            fy = ((fxy[:, 1] - y0) / cell_m).astype(int)
            np.clip(fx, 0, nx - 1, out=fx)
            np.clip(fy, 0, ny - 1, out=fy)
            floor_evidence[fx, fy] = True
        if obs_mask.any():
            oxy = xy[obs_mask]
            ox = ((oxy[:, 0] - x0) / cell_m).astype(int)
            oy = ((oxy[:, 1] - y0) / cell_m).astype(int)
            np.clip(ox, 0, nx - 1, out=ox)
            np.clip(oy, 0, ny - 1, out=oy)
            occ[ox, oy] = True

        # 障碍膨胀：安全余量（盲人行走至少留 ~0.35m 侧隙）
        radius = max(0, round(inflate_m / cell_m))
        grid = OccupancyGrid(cell_m=cell_m, origin=np.array([x0, y0]), shape=(nx, ny), occ=occ, floor_evidence=floor_evidence)
        grid._dilate(radius)
        return grid

    def _dilate(self, radius: int) -> None:
        if radius <= 0:
            return
        blocked = self.occ.copy()
        for _ in range(radius):
            grown = blocked.copy()
            rows, cols = np.nonzero(blocked)
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                r = rows + dx
                c = cols + dy
                valid = (r >= 0) & (r < self.shape[0]) & (c >= 0) & (c < self.shape[1])
                grown[r[valid], c[valid]] = True
            blocked = grown
        self.occ = blocked

    def plan_path(
        self,
        start_xy: np.ndarray,
        goal_xy: np.ndarray,
        *,
        unknown_penalty: float = 0.4,
        turn_penalty: float = 0.2,
    ) -> list[np.ndarray]:
        """A*：返回米制路径点序列（含起终点）；无路返回空列表。"""
        start = self.nearest_free(start_xy)
        goal = self.nearest_free(goal_xy)
        s_cell = self.cell_of(start)
        g_cell = self.cell_of(goal)
        nx, ny = self.shape

        def cost(cell: tuple[int, int]) -> float:
            if self.occ[cell]:
                return math.inf
            return 1.0 if self.floor_evidence[cell] else 1.0 + unknown_penalty

        def h(cell: tuple[int, int]) -> float:
            return math.hypot(cell[0] - g_cell[0], cell[1] - g_cell[1])

        came: dict[tuple[int, int], tuple[int, int] | None] = {s_cell: None}
        g_score: dict[tuple[int, int], float] = {s_cell: 0.0}
        open_heap: list[tuple[float, int, tuple[int, int]]] = [(h(s_cell), 0, s_cell)]
        counter = 1
        while open_heap:
            _, _, cur = heapq.heappop(open_heap)
            if cur == g_cell:
                break
            # 四邻域：室内行走以直角转向为主，播报更符合直觉（左转/右转）。
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cur[0] + dx, cur[1] + dy)
                if not (0 <= nxt[0] < nx and 0 <= nxt[1] < ny):
                    continue
                step_cost = cost(nxt)
                if not math.isfinite(step_cost):
                    continue
                # 转向惩罚：偏好长直段 + 直角转弯，避免 45° 楼梯步，
                # 让逐段播报（直行/左转/右转）更符合盲人行走直觉。
                parent = came.get(cur)
                prev_dir = (
                    (cur[0] - parent[0], cur[1] - parent[1])
                    if parent is not None
                    else None
                )
                turn_cost = turn_penalty if prev_dir is not None and prev_dir != (dx, dy) else 0.0
                tentative = g_score[cur] + step_cost + turn_cost
                if tentative < g_score.get(nxt, math.inf):
                    came[nxt] = cur
                    g_score[nxt] = tentative
                    heapq.heappush(open_heap, (tentative + h(nxt), counter, nxt))
                    counter += 1
        if g_cell not in came:
            return []
        cells: list[tuple[int, int]] = []
        c: tuple[int, int] | None = g_cell
        while c is not None:
            cells.append(c)
            c = came[c]
        cells.reverse()
        return [self.center_of(ix, iy) for ix, iy in cells]


def simplify_waypoints(path: list[np.ndarray], tol_m: float = 0.25) -> list[np.ndarray]:
    """Ramer-Douglas-Peucker：只保留真正的大转折，直行段合并为一条。"""
    pts = [np.asarray(p, dtype=float) for p in path]
    if len(pts) <= 2:
        return pts

    def seg_dist(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        ab = b - a
        length2 = float(ab @ ab)
        if length2 < 1e-12:
            return float(np.linalg.norm(p - a))
        t = float(np.clip(((p - a) @ ab) / length2, 0.0, 1.0))
        return float(np.linalg.norm(p - (a + t * ab)))

    def rdp(indices: list[int]) -> list[int]:
        if len(indices) <= 2:
            return indices
        a, b = pts[indices[0]], pts[indices[-1]]
        dmax, imax = 0.0, -1
        for i in indices[1:-1]:
            d = seg_dist(pts[i], a, b)
            if d > dmax:
                dmax, imax = d, i
        if dmax > tol_m:
            mid = indices.index(imax)
            left = rdp(indices[: mid + 1])
            right = rdp(indices[mid:])
            return left[:-1] + right
        return [indices[0], indices[-1]]

    return [pts[i] for i in rdp(list(range(len(pts))))]


def waypoint_text(points: list[np.ndarray], target: str = "目标") -> str:
    """转折点序列 → 逐段转向播报（直行/左转/右转/调头 + 段长）。"""
    if len(points) < 2:
        return f"已到达{target}"
    segs: list[str] = []
    heading = 0.0
    for i in range(1, len(points)):
        dx, dy = points[i] - points[i - 1]
        dist = float(np.hypot(dx, dy))
        if dist < 1e-6:
            continue
        new_heading = math.atan2(dy, dx)
        if i == 1:
            turn = ""
        else:
            delta = math.degrees(new_heading - heading)
            delta = (delta + 180.0) % 360.0 - 180.0
            turn = (
                "调头后" if abs(delta) > 150.0
                else "左转后" if delta > 45.0
                else "右转后" if delta < -45.0
                else ""
            )
        heading = new_heading
        segs.append(f"{turn}直行约{dist:.1f}米" if turn else f"直行约{dist:.1f}米")
    return "，".join(segs) + f"，到达{target}"
