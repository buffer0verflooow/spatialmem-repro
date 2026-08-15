"""M5.1 无感持续学习：候选池 + novelty 检测 + 同实例合并。

设计原则（计划文档 §4.6）：只积累证据，不急于下结论。
- 候选池：检测到但未进入确认实例的物体 → 低置信度候选（裁剪图 + 3D 锚点 + 时段），
  而不是丢弃（例：白色电风扇只在检测中出现，从未成为记忆节点）；
- novelty 检测：候选与「已知确认节点」判定为同一物体 → 跳过（黑椅子残留检测）；
- 同实例合并：同一物体被拆成多条实例 → 合并（chair_6/chair_8 双实例案例）。

身份判定（真实数据实测）：局部外观特征（HSV 直方图）在灰调背景稀释下分不开
黑椅 vs 白风扇（相似度 0.92），因此 **身份以 VLM 语义（名称+颜色）优先**、
外观特征兜底；M5.4 再升级为视觉嵌入再识别。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


# ---------- 语义（VLM 名称 + 颜色）身份 ----------

# 名称 → 语义族：不同叫法（椅子/办公椅）归一族，族相同才可判「同一物体」。
NAME_FAMILY = {
    "椅子": "chair", "办公椅": "chair", "座椅": "chair",
    "沙发": "sofa", "马桶": "toilet", "风扇": "fan", "落地扇": "fan",
    "电风扇": "fan", "笔记本电脑": "laptop", "电脑": "laptop",
    "显示器": "monitor", "电脑显示器": "monitor",
    "床": "bed", "鼠标": "mouse", "杯子": "cup", "花筒": "vase", "水桶": "bucket",
}


def name_family(name: str) -> str:
    n = (name or "").strip()
    return NAME_FAMILY.get(n, n.lower())


def same_color(c1: Optional[str], c2: Optional[str]) -> bool:
    if not c1 or not c2:
        return True  # 缺颜色不构成反证（保守）
    return (c1 or "").strip() == (c2 or "").strip()


# ---------- 外观特征（兜底信号） ----------


def crop_feature(crop_rgb: np.ndarray) -> np.ndarray:
    """裁剪图 → 128 维 HSV 软直方图 + 12 维四象限主色，拼接 L2 归一化。

    软直方图（h8×s4×v4）：像素按线性权重分配到相邻桶；低饱和像素的色相
    无意义且不稳定，权重均匀摊到 8 个色相桶。中心高斯加权减少背景稀释。
    """
    src = Image.fromarray(np.asarray(crop_rgb, dtype=np.uint8))
    img = src.resize((32, 32), Image.Resampling.BILINEAR)
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]

    yy, xx = np.mgrid[0:32, 0:32]
    d_norm = np.sqrt(((xx - 15.5) / 16.0) ** 2 + ((yy - 15.5) / 16.0) ** 2)
    weight = np.exp(-(d_norm**2) / (2 * 0.45**2)).astype(np.float32)

    ph = np.clip(h / 256.0 * 8.0, 0.0, 7.0 - 1e-6)
    lo_h = np.floor(ph).astype(int)
    hi_h = np.minimum(lo_h + 1, 7)
    w_hi_h = ph - lo_h
    w_lo_h = 1.0 - w_hi_h
    ps_ = np.clip(s / 256.0 * 4.0, 0.0, 3.0 - 1e-6)
    lo_s = np.floor(ps_).astype(int)
    hi_s = np.minimum(lo_s + 1, 3)
    w_hi_s = ps_ - lo_s
    w_lo_s = 1.0 - w_hi_s
    pv_ = np.clip(v / 256.0 * 4.0, 0.0, 3.0 - 1e-6)
    lo_v = np.floor(pv_).astype(int)
    hi_v = np.minimum(lo_v + 1, 3)
    w_hi_v = pv_ - lo_v
    w_lo_v = 1.0 - w_hi_v

    hist = np.zeros(128)

    def scatter_h(h_idx, w_h, mask):
        for s_, ws_ in ((lo_s, w_lo_s), (hi_s, w_hi_s)):
            for v_, wv_ in ((lo_v, w_lo_v), (hi_v, w_hi_v)):
                np.add.at(
                    hist,
                    h_idx[mask] * 16 + s_[mask] * 4 + v_[mask],
                    (w_h * ws_ * wv_ * weight)[mask],
                )

    low_sat = (s / 255.0) < 0.15
    for hb in range(8):
        m = low_sat & (lo_h == hb)
        if m.any():
            scatter_h(np.full_like(lo_h, hb), np.full_like(lo_h, 1.0 / 8.0), m)
    m = ~low_sat
    if m.any():
        scatter_h(lo_h, w_lo_h, m)
        scatter_h(hi_h, w_hi_h, m)
    hist /= max(1.0, hist.sum())

    rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    quad = np.concatenate(
        [
            rgb[:16, :16].mean(axis=(0, 1)),
            rgb[:16, 16:].mean(axis=(0, 1)),
            rgb[16:, :16].mean(axis=(0, 1)),
            rgb[16:, 16:].mean(axis=(0, 1)),
        ]
    )
    feat = np.concatenate([hist, quad])
    norm = np.linalg.norm(feat)
    return feat / max(norm, 1e-9)


def appearance_distance(a: np.ndarray, b: np.ndarray) -> float:
    """1 - cosine 相似度；越小越像。"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    cos = float(np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9))
    return 1.0 - cos


def crop_frame(frames_dir: Path, frame: str, bbox) -> Optional[np.ndarray]:
    """按 bbox 裁剪帧 → RGB ndarray；越界/过小时返回 None。"""
    path = Path(frames_dir) / frame
    if not path.exists():
        return None
    img = Image.open(path).convert("RGB")
    x1, y1, x2, y2 = (int(v) for v in bbox)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img.width, x2), min(img.height, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return np.asarray(img.crop((x1, y1, x2, y2)))


# ---------- 同物体判定 ----------


def is_same_object(
    feat_a: np.ndarray,
    feat_b: np.ndarray,
    *,
    sim_tol: float = 0.82,
    center_a: Optional[tuple] = None,
    center_b: Optional[tuple] = None,
    three_d_tol: float = 1.0,
    window_a: Optional[tuple[int, int]] = None,
    window_b: Optional[tuple[int, int]] = None,
    semantic_a: Optional[tuple[str, str]] = None,
    semantic_b: Optional[tuple[str, str]] = None,
) -> bool:
    """同一物体判定：语义优先，外观兜底。

    - 语义可用（VLM 名称+颜色）：颜色必须一致；名称族相同 或 3D 邻近
      或 时间窗重叠 ≥50% → 同一物体；
    - 语义缺失：回退「外观相似 且（3D 邻近 或 时间窗重叠）」。
    """
    if semantic_a is not None and semantic_b is not None:
        name_a, color_a = semantic_a
        name_b, color_b = semantic_b
        if not same_color(color_a, color_b):
            return False
        if name_family(name_a) == name_family(name_b):
            return True
        if center_a is not None and center_b is not None:
            d = float(
                np.linalg.norm(
                    np.asarray(center_a, dtype=float) - np.asarray(center_b, dtype=float)
                )
            )
            if d <= three_d_tol:
                return True
        if window_a is not None and window_b is not None:
            overlap = min(window_a[1], window_b[1]) - max(window_a[0], window_b[0])
            shorter = min(window_a[1] - window_a[0], window_b[1] - window_b[0])
            if overlap > 0 and shorter > 0 and overlap / shorter >= 0.5:
                return True
        return False

    sim = 1.0 - appearance_distance(feat_a, feat_b)
    if sim < sim_tol:
        return False
    if center_a is not None and center_b is not None:
        d = float(
            np.linalg.norm(
                np.asarray(center_a, dtype=float) - np.asarray(center_b, dtype=float)
            )
        )
        if d <= three_d_tol:
            return True
    if window_a is not None and window_b is not None:
        overlap = min(window_a[1], window_b[1]) - max(window_a[0], window_b[0])
        shorter = min(window_a[1] - window_a[0], window_b[1] - window_b[0])
        if overlap > 0 and shorter > 0 and overlap / shorter >= 0.5:
            return True
    return False


# ---------- 候选池 ----------


@dataclass
class Candidate:
    candidate_id: str
    appearances: list[dict] = field(default_factory=list)
    center: Optional[tuple] = None
    feature: Optional[np.ndarray] = None
    first_seen: str = ""
    last_seen: str = ""
    n_observations: int = 0
    label_hint: str = ""
    semantic: Optional[tuple[str, str]] = None  # (name, color) 来自 VLM
    status: str = "candidate"


class CandidatePool:
    """novelty 检测 + 重复合并的候选池。

    - 与已知确认节点语义相同（颜色一致 + 名称族相同）→ 不是新物体，跳过；
    - 与池内候选为同一物体 → 合并；
    - 否则新建候选。
    """

    def __init__(
        self,
        *,
        known_features: Optional[list[tuple[str, np.ndarray]]] = None,
        known_semantics: Optional[list[tuple[str, Optional[tuple[str, str]]]]] = None,
        sim_tol: float = 0.82,
        three_d_tol: float = 1.0,
    ) -> None:
        self._known = list(known_features or [])
        self._known_sem = list(known_semantics or [])
        self._sim_tol = sim_tol
        self._three_d_tol = three_d_tol
        self._cands: dict[str, Candidate] = {}
        self._seq = 0
        self.stats = {"known_skip": 0, "merged": 0, "new": 0}

    def _window(self, c: Candidate) -> tuple[int, int]:
        def n(f: str) -> int:
            return int(Path(f).stem.split("_")[-1]) if f else 0

        return n(c.first_seen), n(c.last_seen)

    def add_or_merge(
        self,
        *,
        feature: np.ndarray,
        frame: str,
        bbox,
        crop_file: str = "",
        center: Optional[tuple] = None,
        label_hint: str = "",
        semantic: Optional[tuple[str, str]] = None,
    ) -> str:
        """返回候选 id；'' 表示命中已知节点（不是新物体）。"""
        if semantic is not None:
            for _, known_sem in self._known_sem:
                if known_sem is None:
                    continue
                if (
                    same_color(semantic[1], known_sem[1])
                    and name_family(semantic[0]) == name_family(known_sem[0])
                ):
                    self.stats["known_skip"] += 1
                    return ""
        else:
            for _, known_feat in self._known:
                if 1.0 - appearance_distance(feature, known_feat) >= self._sim_tol:
                    self.stats["known_skip"] += 1
                    return ""

        for cid, c in self._cands.items():
            fn = int(Path(frame).stem.split("_")[-1])
            if is_same_object(
                feature,
                c.feature,
                sim_tol=self._sim_tol,
                center_a=center,
                center_b=c.center,
                three_d_tol=self._three_d_tol,
                window_a=(fn, fn),
                window_b=self._window(c),
                semantic_a=semantic,
                semantic_b=c.semantic,
            ):
                c.appearances.append(
                    {"frame": frame, "bbox": [int(v) for v in bbox], "crop_file": crop_file}
                )
                c.last_seen = frame
                c.n_observations += 1
                if center is not None:
                    c.center = center
                if semantic is not None:
                    c.semantic = semantic
                self.stats["merged"] += 1
                return cid

        self._seq += 1
        cid = f"cand_{self._seq}"
        self._cands[cid] = Candidate(
            candidate_id=cid,
            appearances=[
                {"frame": frame, "bbox": [int(v) for v in bbox], "crop_file": crop_file}
            ],
            center=center,
            feature=feature,
            first_seen=frame,
            last_seen=frame,
            n_observations=1,
            label_hint=label_hint,
            semantic=semantic,
        )
        self.stats["new"] += 1
        return cid

    def candidates(self) -> list[Candidate]:
        return list(self._cands.values())


# ---------- 实例去重 ----------


def merge_duplicate_instances(
    instances: list[dict],
    *,
    frames_dir: Path,
    semantics: Optional[dict[str, Optional[tuple[str, str]]]] = None,
    sim_tol: float = 0.82,
    three_d_tol: float = 1.0,
) -> tuple[list[dict], list[dict]]:
    """同一物体被关联成多条实例 → 合并（保留高置信，吸收低置信）。

    返回 (去重后的实例, 合并报告)。判定 = 同类 + 语义（颜色+名称族）
    +（3D 邻近 或 时间窗重叠）。
    """

    def frame_n(s: str) -> int:
        return int(Path(s).stem.split("_")[-1]) if s else 0

    feats: dict[str, Optional[np.ndarray]] = {}
    for inst in instances:
        ev = inst.get("evidence") or {}
        crop = crop_frame(
            frames_dir, ev.get("frame", ""), ev.get("bbox2d") or [0, 0, 0, 0]
        )
        feats[inst["instance_id"]] = crop_feature(crop) if crop is not None else None

    merged = list(instances)
    report: list[dict] = []
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if a["class"] != b["class"]:
                    continue
                fa, fb = feats.get(a["instance_id"]), feats.get(b["instance_id"])
                if fa is None or fb is None:
                    continue
                sem_a = (semantics or {}).get(a["instance_id"])
                sem_b = (semantics or {}).get(b["instance_id"])
                if not is_same_object(
                    fa,
                    fb,
                    sim_tol=sim_tol,
                    center_a=a.get("center"),
                    center_b=b.get("center"),
                    three_d_tol=three_d_tol,
                    window_a=(frame_n(a["first_frame"]), frame_n(a["last_frame"])),
                    window_b=(frame_n(b["first_frame"]), frame_n(b["last_frame"])),
                    semantic_a=sem_a,
                    semantic_b=sem_b,
                ):
                    continue
                keep, drop = (a, b) if a["median_conf"] >= b["median_conf"] else (b, a)
                keep = dict(keep)
                keep["n_observations"] = (
                    keep.get("n_observations", 0) + drop.get("n_observations", 0)
                )
                keep["first_frame"] = min(keep["first_frame"], drop["first_frame"])
                keep["last_frame"] = max(keep["last_frame"], drop["last_frame"])
                keep["merged_from"] = keep.get("merged_from", []) + [drop["instance_id"]]
                merged[j] = merged[-1]
                merged.pop()
                for k, m in enumerate(merged):
                    if m["instance_id"] == keep["instance_id"]:
                        merged[k] = keep
                        break
                report.append(
                    {
                        "keep": keep["instance_id"],
                        "absorbed": drop["instance_id"],
                        "class": keep["class"],
                        "n_observations_sum": keep["n_observations"],
                    }
                )
                changed = True
                break
            if changed:
                break
    return merged, report


__all__ = [
    "name_family",
    "same_color",
    "crop_feature",
    "appearance_distance",
    "crop_frame",
    "is_same_object",
    "Candidate",
    "CandidatePool",
    "merge_duplicate_instances",
]
