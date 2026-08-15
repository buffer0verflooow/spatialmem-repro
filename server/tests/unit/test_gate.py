"""闸门测试。这是成本的主控开关（CLAUDE.md §5.1），覆盖必须最严。"""

from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.gate.node import (
    REASON_DUPLICATE,
    REASON_NO_SCENE_CHANGE,
    REASON_RATE_LIMIT,
    REASON_READ_RATE_LIMIT,
    make_gate_node,
)
from app.gate.phash import dhash, hamming
from app.storage import MemoryKV
from tests.conftest import make_jpeg


def _state(image: bytes, *, device_id: str = "d1", ts: float | None = None, trigger: str = "auto"):
    return {
        "device_id": device_id,
        "frame_jpeg": image,
        "timestamp": ts if ts is not None else time.time(),
        "trigger": trigger,
    }


class TestPhash:
    def test_same_image_zero_distance(self):
        img = make_jpeg(seed=1)
        assert hamming(dhash(img), dhash(img)) == 0

    def test_different_scenes_are_far_apart(self):
        a = dhash(make_jpeg(seed=1))
        b = dhash(make_jpeg(seed=2))
        assert hamming(a, b) > 8

    def test_empty_hash_treated_as_fully_different(self):
        assert hamming("", "abc") == 64
        assert hamming("not-hex", "also-not-hex") == 64

    def test_undecodable_raises(self):
        with pytest.raises(ValueError):
            dhash(b"definitely not a jpeg")


class TestRateLimit:
    async def test_second_call_within_same_second_rejected(self, settings: Settings):
        settings = settings.model_copy(update={"gate_rate_limit_per_sec": 1.0})
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)

        first = await gate(_state(make_jpeg(seed=1)))
        assert first["rejected_by"] is None

        second = await gate(_state(make_jpeg(seed=99)))
        assert second["rejected_by"] == "gate"
        assert second["reject_reason"] == REASON_RATE_LIMIT

    async def test_limit_is_per_device(self, settings: Settings):
        settings = settings.model_copy(update={"gate_rate_limit_per_sec": 1.0})
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)

        assert (await gate(_state(make_jpeg(seed=1), device_id="a")))["rejected_by"] is None
        # 另一台设备有自己的桶，不受影响
        assert (await gate(_state(make_jpeg(seed=1), device_id="b")))["rejected_by"] is None


class TestDeduplication:
    async def test_identical_frame_rejected(self, settings: Settings):
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        img = make_jpeg(seed=1)

        assert (await gate(_state(img)))["rejected_by"] is None
        repeat = await gate(_state(img))
        assert repeat["reject_reason"] == REASON_DUPLICATE
        assert repeat["hash_distance"] == 0

    async def test_different_scene_passes(self, settings: Settings):
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)

        assert (await gate(_state(make_jpeg(seed=1))))["rejected_by"] is None
        assert (await gate(_state(make_jpeg(seed=2))))["rejected_by"] is None

    async def test_first_frame_always_passes(self, settings: Settings):
        """会话首帧没有比较基准，必须放行。"""
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        out = await gate(_state(make_jpeg(seed=5)))
        assert out["rejected_by"] is None
        assert out["hash_distance"] == 64


class TestSceneGate:
    async def test_small_change_within_min_interval_rejected(self, settings: Settings):
        """距上次调用不足 min_interval，且变化不够大 -> 驳回。这是省钱的主力规则。"""
        settings = settings.model_copy(
            update={"gate_min_interval_s": 3.0, "gate_phash_dup_distance": 2,
                    "gate_force_distance": 40}
        )
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        t0 = 1000.0

        assert (await gate(_state(make_jpeg(seed=1), ts=t0)))["rejected_by"] is None
        out = await gate(_state(make_jpeg(seed=2), ts=t0 + 1.0))
        assert out["reject_reason"] == REASON_NO_SCENE_CHANGE
        assert out["since_last_call_s"] == pytest.approx(1.0)

    async def test_large_change_bypasses_min_interval(self, settings: Settings):
        """场景剧变（距离 >= force_distance）必须立即放行，否则会漏掉危险。"""
        settings = settings.model_copy(
            update={"gate_min_interval_s": 3.0, "gate_phash_dup_distance": 2,
                    "gate_force_distance": 1}
        )
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        t0 = 1000.0

        assert (await gate(_state(make_jpeg(seed=1), ts=t0)))["rejected_by"] is None
        out = await gate(_state(make_jpeg(seed=2), ts=t0 + 0.1))
        assert out["rejected_by"] is None

    async def test_after_min_interval_passes(self, settings: Settings):
        settings = settings.model_copy(
            update={"gate_min_interval_s": 3.0, "gate_phash_dup_distance": 2,
                    "gate_force_distance": 40}
        )
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        t0 = 1000.0

        await gate(_state(make_jpeg(seed=1), ts=t0))
        out = await gate(_state(make_jpeg(seed=2), ts=t0 + 5.0))
        assert out["rejected_by"] is None


class TestManualTrigger:
    async def test_manual_bypasses_dedup(self, settings: Settings):
        """用户主动按键，即使画面完全没变也要给结果（§5.1 规则 4）。"""
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        img = make_jpeg(seed=1)

        await gate(_state(img, trigger="manual"))
        out = await gate(_state(img, trigger="manual"))
        assert out["rejected_by"] is None

    async def test_manual_still_rate_limited(self, settings: Settings):
        """豁免去重和场景门控，但不豁免限流——否则连按会打爆配额。"""
        settings = settings.model_copy(update={"gate_rate_limit_per_sec": 1.0})
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        img = make_jpeg(seed=1)

        await gate(_state(img, trigger="manual"))
        out = await gate(_state(img, trigger="manual"))
        assert out["reject_reason"] == REASON_RATE_LIMIT


class TestDecodeFailure:
    async def test_garbage_payload_rejected(self, settings: Settings):
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        out = await gate(_state(b"x" * 2048))
        assert out["reject_reason"] == "decode_failed"


class TestReadMode:
    """阅读模式（trigger=read）。

    豁免去重和场景门控——用户对着同一份菜单拍第二次，dhash 距离必然接近 0，
    不豁免就直接被驳回，功能等于废掉。

    但必须走**独立的**令牌桶：单次阅读的 completion tokens 是普通帧的
    20-30 倍，连按会烧钱；同时它也不该挤占实时帧的限流额度。
    """

    async def test_read_skips_dedup(self, settings: Settings):
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        img = make_jpeg(seed=1)

        await gate(_state(img, trigger="read"))
        out = await gate(_state(img, trigger="read"))
        assert out["rejected_by"] is None

    async def test_read_skips_scene_gate(self, settings: Settings):
        """关掉去重、放开限流，把场景门控单独隔离出来验证。"""
        settings = settings.model_copy(
            update={
                "gate_rate_limit_per_sec": 100.0,
                "gate_phash_dup_distance": 0,  # 关掉去重，只留场景门控
                "gate_min_interval_s": 3600.0,
                "gate_force_distance": 64,  # 任何距离都不足以强制放行
                "gate_read_burst": 5.0,
            }
        )
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        img = make_jpeg(seed=1)

        # 先证明同样条件下 auto 确实会被场景门控挡住
        await gate(_state(img, trigger="auto"))
        blocked = await gate(_state(img, trigger="auto"))
        assert blocked["reject_reason"] == REASON_NO_SCENE_CHANGE

        out = await gate(_state(img, trigger="read"))
        assert out["rejected_by"] is None

    async def test_read_rate_limited_by_own_burst(self, settings: Settings):
        settings = settings.model_copy(
            update={"gate_read_rate_per_min": 6.0, "gate_read_burst": 3.0}
        )
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)
        img = make_jpeg(seed=1)

        for _ in range(3):
            assert (await gate(_state(img, trigger="read")))["rejected_by"] is None

        out = await gate(_state(img, trigger="read"))
        assert out["reject_reason"] == REASON_READ_RATE_LIMIT

    async def test_read_does_not_consume_realtime_budget(self, settings: Settings):
        """阅读模式不能挤掉实时帧的限流额度。"""
        settings = settings.model_copy(
            update={"gate_rate_limit_per_sec": 1.0, "gate_read_burst": 3.0}
        )
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)

        await gate(_state(make_jpeg(seed=1), trigger="read"))
        await gate(_state(make_jpeg(seed=2), trigger="read"))

        out = await gate(_state(make_jpeg(seed=3), trigger="auto"))
        assert out["rejected_by"] is None

    async def test_realtime_traffic_does_not_block_reading(self, settings: Settings):
        """反向：实时帧打满限流时，用户主动发起的阅读仍要能用。"""
        settings = settings.model_copy(update={"gate_rate_limit_per_sec": 1.0})
        kv = MemoryKV()
        gate = make_gate_node(kv, settings)

        await gate(_state(make_jpeg(seed=1), trigger="auto"))
        blocked = await gate(_state(make_jpeg(seed=2), trigger="auto"))
        assert blocked["reject_reason"] == REASON_RATE_LIMIT

        out = await gate(_state(make_jpeg(seed=3), trigger="read"))
        assert out["rejected_by"] is None
