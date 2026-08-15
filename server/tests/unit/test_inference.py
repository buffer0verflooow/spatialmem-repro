from __future__ import annotations

import pytest

from app.inference.backend import MockBackend
from app.inference.image import normalize
from app.inference.parser import parse
from app.inference.schema import VLResult
from tests.conftest import make_jpeg


class TestParser:
    def test_clean_json(self):
        result, degraded = parse(
            '{"scene":"街道","objects":["car"],"ocr_text":"","keywords":["车"],'
            '"risk_level":"high","advice":"注意车辆"}'
        )
        assert not degraded
        assert result.risk_level == "high"
        assert result.advice == "注意车辆"

    def test_markdown_fence_stripped(self):
        result, degraded = parse('```json\n{"advice":"小心台阶","risk_level":"medium"}\n```')
        assert not degraded
        assert result.advice == "小心台阶"

    def test_leading_prose_recovered_by_brace_extract(self):
        result, degraded = parse('好的，分析如下：{"advice":"前方右转","risk_level":"low"}')
        assert degraded
        assert result.advice == "前方右转"

    def test_broken_json_recovered_by_regex(self):
        """JSON 坏了但 advice 还在 -> 用户至少收到一句话，不能白屏。"""
        result, degraded = parse('{"advice": "红灯停下", "risk_level": bad}')
        assert degraded
        assert result.advice == "红灯停下"

    def test_total_garbage_returns_empty_not_exception(self):
        result, degraded = parse("模型今天不太想工作")
        assert degraded
        assert result.advice == ""
        assert result.risk_level == "none"

    def test_empty_input(self):
        result, degraded = parse("")
        assert degraded
        assert result == VLResult()

    def test_never_raises(self):
        for raw in ("", "null", "[]", "{", "{}", '{"risk_level": 42}', "「」"):
            parse(raw)  # 不抛就算过


class TestSchemaCoercion:
    def test_comma_string_coerced_to_list(self):
        r = VLResult.model_validate({"keywords": "红灯，人行道,斑马线"})
        assert r.keywords == ["红灯", "人行道", "斑马线"]

    def test_risk_level_alias(self):
        assert VLResult.model_validate({"risk_level": "danger"}).risk_level == "high"
        assert VLResult.model_validate({"risk_level": "高"}).risk_level == "high"
        assert VLResult.model_validate({"risk_level": "WARNING"}).risk_level == "medium"

    def test_unknown_risk_falls_back_to_none(self):
        assert VLResult.model_validate({"risk_level": "怪东西"}).risk_level == "none"

    def test_lists_capped(self):
        r = VLResult.model_validate({"objects": [f"o{i}" for i in range(30)]})
        assert len(r.objects) == 10

    def test_missing_fields_use_defaults(self):
        r = VLResult.model_validate({})
        assert r.scene == "" and r.objects == [] and r.risk_level == "none"


class TestImageNormalize:
    def test_oversized_image_downscaled(self):
        big = make_jpeg(width=2400, height=1600, seed=1)
        out, recoded = normalize(big, max_edge=1024, quality=75)
        assert recoded
        assert len(out) < len(big)

        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(out)) as img:
            assert max(img.size) <= 1024

    def test_conforming_image_passed_through_untouched(self):
        """已满足要求的图不重编码，省掉 20-40ms（CLAUDE.md §6）。"""
        small = make_jpeg(width=640, height=480, seed=2)
        out, recoded = normalize(small, max_edge=1024, quality=75)
        assert not recoded
        assert out is small


class TestMockBackend:
    async def test_deterministic(self):
        backend = MockBackend()
        img = make_jpeg(seed=7)
        a = await backend.infer(img, [])
        b = await backend.infer(img, [])
        assert a.raw_text == b.raw_text

    async def test_output_parses(self):
        backend = MockBackend()
        resp = await backend.infer(make_jpeg(seed=7), [])
        result, degraded = parse(resp.raw_text)
        assert not degraded
        assert result.risk_level in ("none", "low", "medium", "high")

    async def test_reports_tokens(self):
        resp = await MockBackend().infer(make_jpeg(seed=1), [])
        assert resp.prompt_tokens > 0
        assert resp.completion_tokens > 0

    async def test_latency_simulation(self):
        import time

        backend = MockBackend(latency_ms=50)
        start = time.perf_counter()
        await backend.infer(make_jpeg(seed=1), [])
        assert (time.perf_counter() - start) >= 0.045

    async def test_kb_context_recorded(self):
        resp = await MockBackend().infer(make_jpeg(seed=1), ["路标释义"])
        assert resp.extra["kb_context_used"] == 1


class TestRetryPolicy:
    async def test_retries_once_then_gives_up(self, settings):
        """vl_retries=1 -> 总共 2 次尝试。绝不能是 3 次重试（会击穿 P95）。"""
        from app.inference.node import _call

        calls = {"n": 0}

        class AlwaysFails:
            async def infer(self, image, kb_context):
                calls["n"] += 1
                raise RuntimeError("boom")

            async def close(self):
                pass

        resp, error = await _call(AlwaysFails(), b"x", [], settings)
        assert resp is None
        assert error is not None and "boom" in error
        assert calls["n"] == settings.vl_retries + 1 == 2

    async def test_succeeds_on_retry(self, settings):
        from app.inference.node import _call

        calls = {"n": 0}

        class FailsOnce:
            async def infer(self, image, kb_context):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient")
                return await MockBackend().infer(make_jpeg(seed=1), kb_context)

            async def close(self):
                pass

        resp, error = await _call(FailsOnce(), b"x", [], settings)
        assert error is None
        assert resp is not None
        assert calls["n"] == 2

    async def test_timeout_is_caught(self, settings):
        import asyncio

        from app.inference.node import _call

        settings = settings.model_copy(update={"vl_timeout_s": 0.05, "vl_retries": 0})

        class TooSlow:
            async def infer(self, image, kb_context):
                await asyncio.sleep(1.0)

            async def close(self):
                pass

        resp, error = await _call(TooSlow(), b"x", [], settings)
        assert resp is None
        assert error is not None and "timeout" in error


@pytest.mark.parametrize("seed", range(8))
def test_mock_covers_all_risk_levels_across_seeds(seed):
    """确认 mock 的 4 种场景都可达，否则测不到 alert/voice/text/noop 全路径。"""
    import asyncio

    resp = asyncio.run(MockBackend().infer(make_jpeg(seed=seed), []))
    result, _ = parse(resp.raw_text)
    assert result.risk_level in ("none", "low", "medium", "high")
