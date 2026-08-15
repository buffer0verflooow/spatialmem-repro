"""感知哈希（dHash，64 bit）。

自己实现而不引 imagehash：dHash 只需 PIL，不用拖 numpy/scipy 进来。
原理：缩到 9x8 灰度，比较每行相邻像素大小，得到 64 位指纹。
"""

from __future__ import annotations

import io

from PIL import Image

_WIDTH = 9
_HEIGHT = 8


def dhash(image_bytes: bytes) -> str:
    """返回 16 位十六进制字符串。图像不可解码时抛 ValueError。"""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            small = img.convert("L").resize((_WIDTH, _HEIGHT), Image.Resampling.LANCZOS)
            pixels = list(small.getdata())
    except Exception as exc:
        raise ValueError(f"图像解码失败: {exc}") from exc

    bits = 0
    pos = 0
    for row in range(_HEIGHT):
        base = row * _WIDTH
        for col in range(_WIDTH - 1):
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1 << pos
            pos += 1
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    """两个 dhash 之间的汉明距离，0-64。任一为空返回 64（视为完全不同）。"""
    if not a or not b:
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64
