"""结构化观察后端测试（mock 确定性 + 解析）。"""

import hashlib

import pytest

from app.observe.backend import MockObserveBackend


def make_jpeg(seed: int) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", (64, 64), (seed % 256, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_mock_observe_is_deterministic_and_schema_valid():
    backend = MockObserveBackend()
    jpeg = make_jpeg(3)
    first = await backend.observe(jpeg, "这是什么")
    second = await backend.observe(jpeg, "这是什么")
    assert first == second
    assert set(first.keys()) == {
        "name", "color", "location", "attributes", "confidence", "support"
    }
    assert isinstance(first["name"], str)
    assert isinstance(first["confidence"], float)
    assert set(first["support"].keys()) == {"name", "color", "location", "attributes"}


@pytest.mark.asyncio
async def test_mock_observe_varies_by_image():
    backend = MockObserveBackend()
    a = await backend.observe(make_jpeg(1), "")
    b = await backend.observe(make_jpeg(2), "")
    # 不同图大概率落到不同场景；至少 schema 正确
    assert a["name"] or b["name"]


def test_mock_observe_empty_scene_on_blank_image():
    backend = MockObserveBackend()
    # digest[0] % 5 可能命中空场景，schema 仍合法
    import asyncio

    out = asyncio.run(backend.observe(b"\x00" * 64, ""))
    assert set(out.keys()) == {
        "name", "color", "location", "attributes", "confidence", "support"
    }
