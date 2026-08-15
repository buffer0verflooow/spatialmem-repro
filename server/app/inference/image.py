"""图像预处理。

CLAUDE.md §6 硬要求：长边 <= 1024px、JPEG q=75。
这对模型调用耗时和 token 成本都是线性影响，是仅次于「减少调用次数」的成本杠杆。
眼镜端做不到就服务端补做。
"""

from __future__ import annotations

import io

from PIL import Image


def normalize(image_bytes: bytes, *, max_edge: int, quality: int) -> tuple[bytes, bool]:
    """返回 (处理后字节, 是否实际重编码)。

    已满足要求的图直接原样返回，省掉一次解码+编码（约 20-40ms）。
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.load()
        width, height = img.size
        needs_resize = max(width, height) > max_edge
        needs_convert = img.format != "JPEG" or img.mode not in ("RGB", "L")

        if not needs_resize and not needs_convert:
            return image_bytes, False

        work = img.convert("RGB")
        if needs_resize:
            work.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        work.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), True
