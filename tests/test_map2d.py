"""2D 可通行地图 + 路径规划测试。

验收用例对应真实教室问题：用户在长桌后方，门在对侧，
路径须先向左绕出长桌、沿走廊上行、绕过讲台后到达门口。
"""

import numpy as np

from spatialmem.map2d import OccupancyGrid, simplify_waypoints, waypoint_text


def classroom_cloud() -> np.ndarray:
    """8m x 6m 教室：长桌 + 讲台 + 走廊 + 门口（与真实问题同构）。"""
    pts: list[np.ndarray] = []

    # 地板：密铺自由区域
    xs = np.arange(0.1, 8.0, 0.1)
    ys = np.arange(0.1, 6.0, 0.1)
    gx, gy = np.meshgrid(xs, ys)
    floor = np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, 0.01)], axis=1)
    pts.append(floor)

    def box(x0: float, x1: float, y0: float, y1: float, z: float) -> None:
        bx = np.arange(x0, x1, 0.08)
        by = np.arange(y0, y1, 0.08)
        bxg, byg = np.meshgrid(bx, by)
        pts.append(np.stack([bxg.ravel(), byg.ravel(), np.full(bxg.size, z)], axis=1))

    # 长桌：x 2..7.6，y 1.0..2.0（横在起点与门口之间，右侧几乎贴墙）
    box(2.0, 7.6, 1.0, 2.0, 0.72)
    # 右侧墙：封死长桌右侧通道，强制从左侧绕出（对应真实教室布局）
    box(7.85, 8.0, 0.0, 6.0, 0.5)
    # 讲台：x 3.5..4.5，y 3.5..4.3（走廊与门口之间）
    box(3.5, 4.5, 3.5, 4.3, 0.35)
    return np.vstack(pts)


def test_classroom_path_avoids_table_and_podium():
    grid = OccupancyGrid.build(classroom_cloud(), cell_m=0.2, inflate_m=0.35)
    start = np.array([4.0, 0.3])  # 长桌后方（距桌边约 0.7m，属正常站立位）
    goal = np.array([4.0, 5.5])  # 门口
    path = grid.plan_path(start, goal)
    assert len(path) > 2, "教室场景应存在可通行路径"

    # 路径不得穿过长桌/讲台本体（膨胀后的安全格也不得进入障碍内部）
    for p in path:
        x, y = float(p[0]), float(p[1])
        in_table = 2.2 <= x <= 5.8 and 1.2 <= y <= 1.8
        in_podium = 3.7 <= x <= 4.3 and 3.7 <= y <= 4.1
        assert not in_table, f"路径穿过长桌: {p}"
        assert not in_podium, f"路径穿过讲台: {p}"

    # 必须从长桌左侧绕出（y 在桌带内时 x < 2）
    assert any(
        1.0 <= float(p[1]) <= 2.0 and float(p[0]) < 2.0 for p in path
    ), "未从长桌左侧绕出"
    # 必须绕过讲台左侧（y 在讲台带内时 x < 3.5）
    assert any(
        3.5 <= float(p[1]) <= 4.3 and float(p[0]) < 3.5 for p in path
    ), "未从讲台左侧绕过"
    # 转折点不少于 3 个（左-右-左 或类似序列）
    waypoints = simplify_waypoints(path)
    assert len(waypoints) >= 3, f"转折点过少: {len(waypoints)}"


def test_inflation_blocks_narrow_gap():
    """两障碍之间留 0.3m 窄缝（< 2x 安全余量）→ 不可通行。"""
    pts: list[np.ndarray] = []
    xs = np.arange(0.1, 5.0, 0.1)
    ys = np.arange(0.1, 3.0, 0.1)
    gx, gy = np.meshgrid(xs, ys)
    pts.append(np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, 0.01)], axis=1))

    def bar(y0: float) -> None:
        bx = np.arange(0.1, 5.0, 0.08)
        by = np.arange(y0, y0 + 0.3, 0.08)
        bxg, byg = np.meshgrid(bx, by)
        pts.append(np.stack([bxg.ravel(), byg.ravel(), np.full(bxg.size, 0.7)], axis=1))

    bar(1.0)  # 障碍带 1：y 1.0..1.3
    bar(1.6)  # 障碍带 2：y 1.6..1.9，中间缝 y 1.3..1.6=0.3m
    grid = OccupancyGrid.build(np.vstack(pts), cell_m=0.2, inflate_m=0.35)
    path = grid.plan_path(np.array([2.5, 0.3]), np.array([2.5, 2.7]))
    assert path == [], "窄缝应被安全余量封闭，返回无路径"


def test_simplify_removes_collinear():
    path = [
        np.array([0.0, 0.0]),
        np.array([0.2, 0.0]),
        np.array([0.4, 0.0]),
        np.array([0.6, 0.0]),
        np.array([0.6, 0.3]),
        np.array([0.6, 0.6]),
    ]
    out = simplify_waypoints(path, tol_m=0.35)
    assert len(out) == 3  # 起点、转折、终点
    np.testing.assert_allclose(out[0], [0.0, 0.0])
    np.testing.assert_allclose(out[1], [0.6, 0.0])
    np.testing.assert_allclose(out[-1], [0.6, 0.6])


def test_waypoint_text_gives_turns():
    path = [np.array([0.0, 0.0]), np.array([3.0, 0.0]), np.array([3.0, 2.0])]
    text = waypoint_text(path, "门口")
    assert "直行约3.0米" in text
    assert ("左转后" in text) or ("右转后" in text)
    assert text.endswith("到达门口")


def test_cell_of_is_inverse_of_center_of():
    """回归：round（银行家舍入）会把奇数格中心映射到相邻格，
    导致 center_of(cell)->cell_of 不自洽、A* 起点终点错位。
    必须 floor（所在格语义）。"""
    grid = OccupancyGrid.build(classroom_cloud(), cell_m=0.2, inflate_m=0.35)
    for ix in (0, 1, 2, 10, 11, 30):
        for iy in (0, 1, 5, 17):
            if ix >= grid.shape[0] or iy >= grid.shape[1]:
                continue
            center = grid.center_of(ix, iy)
            assert grid.cell_of(center) == (ix, iy), f"cell_of(center_of({ix},{iy})) 不自洽"
