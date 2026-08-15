"""前置规则：格式 / 尺寸 / 模糊 / 敏感场景。纯确定性代码，不含模型调用。"""

from __future__ import annotations

import io
import statistics
from dataclasses import dataclass

from PIL import Image, ImageFilter

MIN_BYTES = 512
MAX_BYTES = 4 * 1024 * 1024
MIN_EDGE = 64
BLUR_VARIANCE_THRESHOLD = 60.0
_ANALYSIS_EDGE = 256  # 模糊检测前先降采样，控制耗时
_EDGE_CROP = 2  # FIND_EDGES 的边界 artifact 宽度，必须裁掉


@dataclass(slots=True)
class PreCheck:
    ok: bool
    reason: str = ""
    detail: str = ""


def check_payload_size(image_bytes: bytes) -> PreCheck:
    n = len(image_bytes)
    if n < MIN_BYTES:
        return PreCheck(False, "too_small", f"{n} bytes")
    if n > MAX_BYTES:
        return PreCheck(False, "too_large", f"{n} bytes")
    return PreCheck(True)


def check_decodable(image_bytes: bytes) -> PreCheck:
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.verify()
    except Exception as exc:
        return PreCheck(False, "decode_failed", str(exc))
    return PreCheck(True)


def check_dimensions(image_bytes: bytes) -> PreCheck:
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
    except Exception as exc:
        return PreCheck(False, "decode_failed", str(exc))
    if min(w, h) < MIN_EDGE:
        return PreCheck(False, "too_low_resolution", f"{w}x{h}")
    return PreCheck(True)


def blur_score(image_bytes: bytes) -> float:
    """边缘图像素方差，越小越模糊。

    用 PIL FIND_EDGES + statistics.pvariance 实现，避免为一个指标引入 numpy。

    注意必须裁掉边框：FIND_EDGES 在图像四边会产生亮边 artifact（卷积核在
    边界上无邻域），纯色图的方差会因此虚高到 ~290，把模糊检测彻底失效。
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        gray = img.convert("L")
        gray.thumbnail((_ANALYSIS_EDGE, _ANALYSIS_EDGE), Image.Resampling.BILINEAR)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        w, h = edges.size
        if w > 2 * _EDGE_CROP and h > 2 * _EDGE_CROP:
            edges = edges.crop((_EDGE_CROP, _EDGE_CROP, w - _EDGE_CROP, h - _EDGE_CROP))
        pixels = list(edges.getdata())
    if len(pixels) < 2:
        return 0.0
    return float(statistics.pvariance(pixels))


def check_not_blurry(image_bytes: bytes, threshold: float = BLUR_VARIANCE_THRESHOLD) -> PreCheck:
    try:
        score = blur_score(image_bytes)
    except Exception as exc:
        return PreCheck(False, "decode_failed", str(exc))
    if score < threshold:
        return PreCheck(False, "too_blurry", f"variance={score:.1f}")
    return PreCheck(True)


def run_static_checks(image_bytes: bytes) -> PreCheck:
    """按代价从低到高的顺序跑，先失败先返回。"""
    for check in (check_payload_size, check_decodable, check_dimensions, check_not_blurry):
        result = check(image_bytes)
        if not result.ok:
            return result
    return PreCheck(True)
