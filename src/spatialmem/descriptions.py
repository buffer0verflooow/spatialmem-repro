"""M3 描述层：物体属性（颜色/尺寸）+ 关系文本 + 双层合并。

遵循 SpatialMem（arXiv:2601.14895v2）的双层描述约定：
- Layer 1（图像级）：绑定当前视角，随观察变化（颜色/尺寸/相对用户方位）；
- Layer 2（场景级）：多视角一致后才写入，保守合并防漂移。

颜色从实例裁剪图（detections.jsonl bbox + frames/）用 HSV 中位数量化得到；
尺寸从度量 3D 框（instances box3d）计算；方位按查询时用户位姿输出。
"""

from __future__ import annotations

import statistics
from typing import Optional

import numpy as np
from PIL import Image

from .memory import Node, SpatialMemory
from .relations import egocentric_direction

# ---------- 中文标签 / 方位 ----------

LABEL_ZH = {
    "cup": "杯子",
    "mug": "马克杯",
    "bottle": "瓶子",
    "laptop": "笔记本电脑",
    "mouse": "鼠标",
    "keyboard": "键盘",
    "remote": "遥控器",
    "cell phone": "手机",
    "chair": "椅子",
    "sofa": "沙发",
    "table": "桌子",
    "dining table": "餐桌",
    "trash can": "垃圾桶",
    "trash bin": "垃圾桶",
    "bucket": "水桶",
    "plastic bucket": "水桶",
    "water bucket": "水桶",
    "pillow": "枕头",
    "blanket": "被子",
    "tv": "电视",
    "support_surface": "桌面",
    "wall": "墙面",
    "indoor_scene": "室内",
}


def zh_label(label: str) -> str:
    return LABEL_ZH.get(label, label)


DIRECTION_ZH = {
    "front": "前方",
    "left": "左方",
    "right": "右方",
    "behind": "后方",
    "left_front": "左前方",
    "right_front": "右前方",
}


def zh_direction(tag: str | None) -> str | None:
    return DIRECTION_ZH.get(tag or "", tag)


# ---------- 颜色 ----------

_HUE_RANGES = [
    (0, 15, "红色"),
    (15, 45, "橙色"),
    (45, 70, "黄色"),
    (70, 160, "绿色"),
    (160, 200, "青色"),
    (200, 255, "蓝色"),
    (255, 285, "紫色"),
    (285, 330, "粉色"),
    (330, 360, "红色"),
]


def dominant_color_name(rgb: np.ndarray) -> str:
    """实例裁剪图（HxWx3 RGB 0-255）→ 中文颜色名。

    缩放到 48×48 后取 HSV 中位数（对噪声鲁棒）；低饱和/低亮归为黑白灰。
    """
    img = Image.fromarray(np.asarray(rgb, dtype=np.uint8)).resize(
        (48, 48), Image.Resampling.BILINEAR
    )
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32)
    h = float(np.median(hsv[..., 0])) * 360.0 / 255.0
    s = float(np.median(hsv[..., 1])) / 255.0
    v = float(np.median(hsv[..., 2])) / 255.0
    if v < 0.12:
        return "黑色"
    if s < 0.15:
        if v > 0.82:
            return "白色"
        return "浅灰色" if v > 0.55 else "深灰色"
    for lo, hi, name in _HUE_RANGES:
        if lo <= h < hi:
            return name
    return "彩色"


# ---------- 尺寸 ----------


def box_dims_cm(box: tuple) -> tuple[int, int, int]:
    """度量 3D 框 → (L, W, H) 厘米，按数值降序；最小 1cm 防零。"""
    ext = sorted(
        (abs(box[3] - box[0]), abs(box[4] - box[1]), abs(box[5] - box[2])),
        reverse=True,
    )
    return tuple(max(1, round(v * 100)) for v in ext)


def size_text_cm(box: tuple) -> str:
    l, w, h = box_dims_cm(box)
    return f"约 {l}×{w}×{h} 厘米"


# ---------- Layer 1（图像级视图描述） ----------


def view_description(
    label: str,
    *,
    color: Optional[str],
    box: Optional[tuple],
    direction_tag: Optional[str],
    distance_m: Optional[float],
) -> str:
    """当前视角下的单句描述："白色的杯子，约 9×8×6 厘米，在你左前方约 1.2 米"。"""
    head = f"{color}的{label}" if color else label
    parts = [head]
    if box is not None:
        parts.append(size_text_cm(box))
    if direction_tag is not None and distance_m is not None:
        parts.append(f"在你{direction_tag}约 {distance_m:.1f} 米")
    return "，".join(parts)


# ---------- Layer 2（场景级保守合并） ----------


class DescriptionAccumulator:
    """跨视角合并：颜色取窗口内多数一致值，尺寸取中位数。

    保守策略：单个属性必须在最近 [window] 个观察里出现 [min_confirmations]
    次且值一致才写入 Layer 2，避免遮挡/光照抖动污染稳定描述。
    """

    def __init__(self, window: int = 3, min_confirmations: int = 2) -> None:
        self._window = max(1, window)
        self._min_conf = max(1, min_confirmations)
        self._colors: dict[str, list[str]] = {}
        self._sizes: dict[str, list[tuple[int, int, int]]] = {}

    def observe(
        self,
        node_id: str,
        color: Optional[str],
        size: Optional[tuple[int, int, int]],
    ) -> None:
        if color:
            q = self._colors.setdefault(node_id, [])
            q.append(color)
            if len(q) > self._window:
                del q[0]
        if size:
            q = self._sizes.setdefault(node_id, [])
            q.append(size)
            if len(q) > self._window:
                del q[0]

    def layer2(self, node_id: str, label: str) -> str:
        """达成共识才返回："白色的杯子，约 9×8×6 厘米"；否则返回空串。"""
        parts: list[str] = []
        colors = self._colors.get(node_id, [])
        if len(colors) >= self._min_conf and len(set(colors)) == 1:
            parts.append(f"{colors[-1]}的{label}")
        sizes = self._sizes.get(node_id, [])
        if len(sizes) >= self._min_conf:
            med = tuple(
                int(round(statistics.median(vals))) for vals in zip(*sizes)
            )
            parts.append(f"约 {med[0]}×{med[1]}×{med[2]} 厘米")
        return "，".join(parts)


# ---------- 关系文本 ----------

_PREDICATE_TEXT = {
    "on": "在{}上",
    "above": "在{}上方",
    "below": "在{}下方",
    "near": "在{}附近",
    "in": "在{}里",
    "left_of": "在{}左边",
    "right_of": "在{}右边",
    "front_of": "在{}前方",
    "behind": "在{}后面",
}


def relation_text(memory: SpatialMemory, node_id: str, max_rels: int = 3) -> str:
    """活动出边关系 → 中文描述，按置信度降序取前 [max_rels] 条。

    只描述「本节点 → 其他节点」的关系（r.subject == node_id），避免把
    「X 在杯子上方」这种入边误读成「杯子在 X 上方」；按 (谓词, 对象) 去重。
    """
    rels = sorted(memory.relations_of(node_id), key=lambda r: -r.confidence)
    out: list[str] = []
    seen: set[tuple] = set()
    for rel in rels:
        if (
            rel.status != "active"
            or rel.subject != node_id
            or rel.object == node_id
            or rel.predicate not in _PREDICATE_TEXT
        ):
            continue
        obj = memory.get_node(rel.object)
        if obj is None or obj.node_type == "room":
            continue
        key = (rel.predicate, obj.label)
        if key in seen:
            continue
        seen.add(key)
        out.append(_PREDICATE_TEXT[rel.predicate].format(zh_label(obj.label)))
        if len(out) >= max_rels:
            break
    return "；".join(out)


# ---------- 位姿工具 ----------


def pose_from_quat(
    qx: float, qy: float, qz: float, qw: float, tx: float, ty: float, tz: float
) -> np.ndarray:
    """(qx,qy,qz,qw) 四元数 + 平移 → 4x4 world→camera 位姿（z 向上）。"""
    qw, qx, qy, qz = qw, qx, qy, qz
    R = np.array(
        [
            [
                1 - 2 * (qy**2 + qz**2),
                2 * (qx * qy - qz * qw),
                2 * (qx * qz + qy * qw),
            ],
            [
                2 * (qx * qy + qz * qw),
                1 - 2 * (qx**2 + qz**2),
                2 * (qy * qz - qx * qw),
            ],
            [
                2 * (qx * qz - qy * qw),
                2 * (qy * qz + qx * qw),
                1 - 2 * (qx**2 + qy**2),
            ],
        ]
    )
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = (tx, ty, tz)
    return T


__all__ = [
    "zh_label",
    "zh_direction",
    "dominant_color_name",
    "box_dims_cm",
    "size_text_cm",
    "view_description",
    "DescriptionAccumulator",
    "relation_text",
    "pose_from_quat",
]
