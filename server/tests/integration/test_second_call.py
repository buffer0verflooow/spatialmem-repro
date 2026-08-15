"""§5.3 例外路径：高风险 + 无预取上下文时的复核调用。

这是唯一被允许的第二次模型调用，占比必须 <5%（§12）。
它既要真的生效（高风险场景不能因为首帧没上下文就给出低质量结论），
又不能在不该触发时触发——两个方向都得测。
"""

from __future__ import annotations

import json

from app.config import Settings
from app.runtime import AppContext
from app.storage import keys as k
from tests.conftest import make_jpeg


class FakeKb:
    """可控知识库：记录被检索的次数，返回预设命中。"""

    def __init__(self, hits: list[str]) -> None:
        self._hits = hits
        self.searches: list[str] = []

    async def search(self, query: str, top_k: int, min_score: float) -> list[str]:
        self.searches.append(query)
        return list(self._hits)

    async def reload(self, persist_dir: str) -> int:
        return len(self._hits)

    @property
    def ready(self) -> bool:
        return True


class CountingBackend:
    """统计调用次数，并让首次结果为 high risk 以触发复核。"""

    MODEL = "counting-vl"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def infer(self, image_jpeg: bytes, kb_context: list[str]):
        from app.inference.backend import VLResponse

        self.calls.append(list(kb_context))
        # 第二次调用（带上下文）给出更精确的建议
        advice = "红灯，禁止通行" if kb_context else "红灯，请等待"
        payload = {
            "scene": "路口",
            "objects": ["traffic_light"],
            "ocr_text": "",
            "keywords": ["红灯"],
            "risk_level": "high",
            "advice": advice,
        }
        return VLResponse(
            raw_text=json.dumps(payload, ensure_ascii=False),
            model=self.MODEL,
            prompt_tokens=100,
            completion_tokens=30,
            latency_ms=10,
        )

    async def close(self) -> None:
        return None


def _rebuild(ctx: AppContext) -> None:
    from app.graph import build_pipeline

    ctx.pipeline = build_pipeline(
        kv=ctx.kv, kb=ctx.kb, backend=ctx.backend, face=ctx.face,
        settings=ctx.settings, spawn=ctx.background.spawn,
    )


async def _make_ctx(settings: Settings, kb_hits: list[str]):
    ctx = AppContext(settings)
    ctx.backend = CountingBackend()
    ctx.kb = FakeKb(kb_hits)
    _rebuild(ctx)
    await ctx.startup()
    return ctx


class TestSecondCallTriggers:
    async def test_high_risk_without_context_triggers_recheck(self, settings: Settings):
        ctx = await _make_ctx(settings, ["红灯：禁止通行，等待绿灯"])
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-sc", frame_jpeg=make_jpeg(seed=1)
            )
            assert len(ctx.backend.calls) == 2, "高风险且无上下文时应复核一次"
            assert ctx.backend.calls[0] == []  # 首次无上下文
            assert ctx.backend.calls[1] == ["红灯：禁止通行，等待绿灯"]  # 复核带上下文
            assert state["vl_meta"]["second_call"] is True
            assert state["reply"]["content"] == "红灯，禁止通行"  # 用的是复核结果
        finally:
            await ctx.shutdown()

    async def test_tokens_and_latency_accumulated(self, settings: Settings):
        """复核的成本必须累加，否则 §7 的成本核算会低估。"""
        ctx = await _make_ctx(settings, ["红灯释义"])
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-sc2", frame_jpeg=make_jpeg(seed=1)
            )
            meta = state["vl_meta"]
            assert meta["prompt_tokens"] == 200  # 100 x 2
            assert meta["completion_tokens"] == 60  # 30 x 2
        finally:
            await ctx.shutdown()


class TestSecondCallSuppressed:
    async def test_no_recheck_when_context_already_present(self, settings: Settings):
        """已有预取上下文就不该复核——这是 §5.2 预取机制存在的意义。"""
        ctx = await _make_ctx(settings, ["红灯释义"])
        try:
            await ctx.kv.set(
                k.kb_ctx("dev-sc3"), json.dumps(["上一帧预取的红灯释义"]), ttl_s=60
            )
            state, _ = await ctx.process_frame(
                device_id="dev-sc3", frame_jpeg=make_jpeg(seed=1)
            )
            assert len(ctx.backend.calls) == 1
            assert state["vl_meta"]["second_call"] is False
        finally:
            await ctx.shutdown()

    async def test_no_recheck_when_kb_returns_nothing(self, settings: Settings):
        """知识库查不到东西时，复核毫无意义，白花一次调用。"""
        ctx = await _make_ctx(settings, [])
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-sc4", frame_jpeg=make_jpeg(seed=1)
            )
            assert len(ctx.backend.calls) == 1
            assert state["vl_meta"]["second_call"] is False
        finally:
            await ctx.shutdown()

    async def test_disabled_by_config(self, settings: Settings):
        """占比超 5% 时要能一键关掉（§5.3）。"""
        settings = settings.model_copy(update={"second_call_enabled": False})
        ctx = await _make_ctx(settings, ["红灯释义"])
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-sc5", frame_jpeg=make_jpeg(seed=1)
            )
            assert len(ctx.backend.calls) == 1
            assert state["vl_meta"]["second_call"] is False
        finally:
            await ctx.shutdown()

    async def test_low_risk_never_rechecks(self, settings: Settings):
        class LowRisk(CountingBackend):
            async def infer(self, image_jpeg: bytes, kb_context: list[str]):
                from app.inference.backend import VLResponse

                self.calls.append(list(kb_context))
                return VLResponse(
                    raw_text=json.dumps(
                        {"risk_level": "low", "advice": "前方书店", "keywords": ["书店"]},
                        ensure_ascii=False,
                    ),
                    model="low-vl",
                )

        ctx = AppContext(settings)
        ctx.backend = LowRisk()
        ctx.kb = FakeKb(["书店释义"])
        _rebuild(ctx)
        await ctx.startup()
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-sc6", frame_jpeg=make_jpeg(seed=1)
            )
            assert len(ctx.backend.calls) == 1
            assert state["vl_meta"]["second_call"] is False
        finally:
            await ctx.shutdown()


class TestRecheckFailureFallback:
    async def test_first_result_kept_when_recheck_fails(self, settings: Settings):
        """复核失败不能丢掉首次的有效结果——高风险场景必须给出提示。"""

        class FailsOnRecheck(CountingBackend):
            async def infer(self, image_jpeg: bytes, kb_context: list[str]):
                if kb_context:  # 复核调用
                    self.calls.append(list(kb_context))
                    raise RuntimeError("recheck upstream down")
                return await super().infer(image_jpeg, kb_context)

        ctx = AppContext(settings)
        ctx.backend = FailsOnRecheck()
        ctx.kb = FakeKb(["红灯释义"])
        _rebuild(ctx)
        await ctx.startup()
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-sc7", frame_jpeg=make_jpeg(seed=1)
            )
            assert state["error"] is None
            assert state["reply"]["type"] == "alert"
            assert state["reply"]["content"] == "红灯，请等待"  # 首次结果
            assert state["vl_meta"]["second_call"] is False
        finally:
            await ctx.shutdown()
