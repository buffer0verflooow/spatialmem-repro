from __future__ import annotations

import io
import random

import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.runtime import AppContext


def make_jpeg(*, width: int = 320, height: int = 240, seed: int = 0) -> bytes:
    """生成可控的测试图：同 seed 必然同图，不同 seed 结构上明显不同。

    不要用棋盘格——那是 dHash 的退化输入（dHash 比较行内相邻像素，
    反相棋盘和原图几乎同构，汉明距离只有个位数）。这里用带种子的
    伪随机矩形+椭圆，既保证结构差异，也保证边缘方差远超模糊阈值。
    """
    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), (10, 10, 10))
    draw = ImageDraw.Draw(img)
    for _ in range(14):
        x0 = rng.randrange(0, width - 20)
        y0 = rng.randrange(0, height - 20)
        x1 = x0 + rng.randrange(20, 90)
        y1 = y0 + rng.randrange(20, 90)
        color = (rng.randrange(90, 256), rng.randrange(90, 256), rng.randrange(90, 256))
        if rng.random() < 0.5:
            draw.rectangle([x0, y0, x1, y1], fill=color)
        else:
            draw.ellipse([x0, y0, x1, y1], fill=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def blurry_jpeg(width: int = 320, height: int = 240) -> bytes:
    """纯色图，边缘方差接近 0，必然被 too_blurry 拦下。"""
    img = Image.new("RGB", (width, height), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def settings() -> Settings:
    """测试配置：全部走内存 + mock，零外部依赖。"""
    return Settings(
        env="test",
        log_level="WARNING",
        kv_backend="memory",
        db_backend="null",
        kb_backend="null",
        inference_backend="mock",
        mock_latency_ms=0,
        device_shared_secret="test-secret",
        gate_rate_limit_per_sec=1000.0,  # 单测默认不限流，需要时单独覆盖
        gate_min_interval_s=0.0,
        gate_phash_dup_distance=8,
        gate_force_distance=16,
    )


@pytest.fixture
async def ctx(settings: Settings):
    context = AppContext(settings)
    await context.startup()
    try:
        yield context
    finally:
        await context.shutdown()
