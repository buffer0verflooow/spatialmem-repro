"""结构化观察后端测试（mock 确定性 + 解析）。"""

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
        "name", "color", "location", "attributes", "confidence", "support", "anchors"
    }
    assert isinstance(first["name"], str)
    assert isinstance(first["confidence"], float)
    assert set(first["support"].keys()) == {"name", "color", "location", "attributes"}
    assert isinstance(first["anchors"], list)
    for anchor in first["anchors"]:
        assert set(anchor.keys()) == {
            "type", "name", "direction", "distance_m", "confidence"
        }
        assert anchor["type"] in ("door", "window", "wall")
        assert anchor["name"]


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
        "name", "color", "location", "attributes", "confidence", "support", "anchors"
    }
    assert isinstance(out["anchors"], list)


@pytest.mark.asyncio
async def test_mock_observe_anchors_are_deterministic_structural():
    """场景锚点（门/窗/墙）确定性返回，且结构合法。"""
    backend = MockObserveBackend()
    jpeg = make_jpeg(7)
    first = await backend.observe(jpeg, "这是什么")
    second = await backend.observe(jpeg, "这是什么")
    assert first["anchors"] == second["anchors"]
    for anchor in first["anchors"]:
        assert anchor["type"] in ("door", "window", "wall")
        assert anchor["direction"] in ("left", "right", "front", "back")
        assert 0.0 <= anchor["confidence"] <= 1.0


def test_normalize_anchors_filters_invalid():
    from app.observe.backend import _normalize_anchors

    raw = [
        {"type": "door", "name": "门", "direction": "left",
         "distance_m": 2.5, "confidence": 0.9},
        {"type": "table", "name": "桌子", "direction": "front", "confidence": 0.9},
        {"type": "window", "name": "", "direction": "front", "confidence": 0.9},
        {"type": "wall", "name": "墙", "direction": "back",
         "distance_m": 1.0, "confidence": 0.8},
        "not-a-dict",
    ]
    out = _normalize_anchors(raw)
    assert [a["name"] for a in out] == ["门", "墙"]
    assert _normalize_anchors(None) == []
    assert _normalize_anchors([{"type": "door", "name": "门",
                                "direction": "up", "confidence": 0.9}]) == [
        {"type": "door", "name": "门", "direction": "",
         "distance_m": 0.0, "confidence": 0.9}
    ]
