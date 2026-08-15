"""画质门控：VLM 输入前的前置闸门（2026-08-11 实验结论落地）。

实验（multiview_quality.json）结论：分辨率 640→320→160 时 VLM 全对率
3/4→1/4→0/4，且低画质诱发物体幻觉（编造鼠标/键盘/显示器）。因此低质量帧
**宁可拒答，不让 VLM 猜测**。

阈值（与客户端画质分析器对齐）：
- dark_mean_threshold = 35（平均亮度）
- overexposed_ratio_threshold = 0.35（luma ≥ 245 占比）
- occluded_dark_ratio_threshold = 0.85（luma ≤ 12 占比）
- blur_variance_threshold = 65（Laplacian 方差，step 采样）
- 分辨率门限按实验数据：短边 ≥ 320 且长边 ≥ 480 才允许进 VLM
  （320×180 已开始幻觉，640×360 正常）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image


@dataclass
class QualityPolicy:
    min_short_edge: int = 320
    min_long_edge: int = 480
    dark_mean_threshold: float = 35.0
    overexposed_ratio_threshold: float = 0.35
    occluded_dark_ratio_threshold: float = 0.85
    blur_variance_threshold: float = 65.0
    sample_step: int = 4
    bright_luma: int = 245
    dark_luma: int = 12


DEFAULT_POLICY = QualityPolicy()


@dataclass
class FrameQuality:
    width: int
    height: int
    mean_luma: float
    dark_ratio: float
    bright_ratio: float
    blur_variance: float
    flags: list[str] = field(default_factory=list)

    def acceptable(self, policy: QualityPolicy = DEFAULT_POLICY) -> tuple[bool, list[str]]:
        """返回 (是否可进 VLM, 不通过的原因列表)。"""
        reasons: list[str] = []
        if (
            min(self.width, self.height) < policy.min_short_edge
            or max(self.width, self.height) < policy.min_long_edge
        ):
            reasons.append("resolution_too_small")
        if self.mean_luma < policy.dark_mean_threshold:
            reasons.append("too_dark")
        if self.bright_ratio > policy.overexposed_ratio_threshold:
            reasons.append("over_exposed")
        if self.dark_ratio > policy.occluded_dark_ratio_threshold:
            reasons.append("occluded")
        if self.blur_variance < policy.blur_variance_threshold:
            reasons.append("blurry")
        return (not reasons, reasons)


def evaluate(jpeg: bytes, policy: QualityPolicy = DEFAULT_POLICY) -> FrameQuality:
    """对 JPEG 帧计算画质指标（Laplacian 方差 + 亮度/曝光统计，step 采样）。"""
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    w, h = img.size
    gray = np.asarray(img, dtype=float)

    s = policy.sample_step
    center = gray[s : h - s : s, s : w - s : s]
    up = gray[0 : h - 2 * s : s, s : w - s : s]
    down = gray[2 * s :: s, s : w - s : s]
    left = gray[s : h - s : s, 0 : w - 2 * s : s]
    right = gray[s : h - s : s, 2 * s :: s]
    lap = 4.0 * center - up - down - left - right

    quality = FrameQuality(
        width=w,
        height=h,
        mean_luma=float(center.mean()),
        dark_ratio=float((center <= policy.dark_luma).mean()),
        bright_ratio=float((center >= policy.bright_luma).mean()),
        blur_variance=float(np.var(lap)),
    )
    _, reasons = quality.acceptable(policy)
    quality.flags = reasons
    return quality


def is_acceptable(jpeg: bytes, policy: QualityPolicy = DEFAULT_POLICY) -> bool:
    return evaluate(jpeg, policy).acceptable(policy)[0]


__all__ = [
    "QualityPolicy",
    "FrameQuality",
    "DEFAULT_POLICY",
    "evaluate",
    "is_acceptable",
]
