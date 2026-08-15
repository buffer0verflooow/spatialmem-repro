from __future__ import annotations

import io
import random

import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.runtime import AppContext


def make_jpeg(*, width: int = 320, height: int = 240, seed: int = 0) -> bytes:
    """生成可控的测试图：同 seed 必然同图，不同 seed 结构上明显不同。"""
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


@pytest.fixture
def settings() -> Settings:
    """测试配置：mock 后端，零外部依赖。"""
    return Settings(
        env="test",
        log_level="WARNING",
        inference_backend="mock",
        mock_latency_ms=0,
    )


@pytest.fixture
async def ctx(settings: Settings):
    context = AppContext(settings)
    await context.startup()
    try:
        yield context
    finally:
        await context.shutdown()
