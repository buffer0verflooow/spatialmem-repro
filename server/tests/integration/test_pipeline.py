"""全链路集成测试。这是 CLAUDE.md §11 里 W1 的出口条件。"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.runtime import AppContext
from tests.conftest import blurry_jpeg, make_jpeg


class TestHappyPath:
    async def test_frame_produces_reply(self, ctx: AppContext):
        state, elapsed = await ctx.process_frame(
            device_id="dev-1", frame_jpeg=make_jpeg(seed=1)
        )
        assert state["rejected_by"] is None
        assert state["error"] is None
        assert state["vl_result"] is not None
        reply = state["reply"]
        assert reply["type"] in ("text", "voice", "alert", "noop")
        assert len(reply["content"]) <= ctx.settings.reply_max_chars
        assert elapsed >= 0

    async def test_all_pipeline_stages_populated(self, ctx: AppContext):
        state, _ = await ctx.process_frame(device_id="dev-1", frame_jpeg=make_jpeg(seed=2))
        assert state["phash"]  # 闸门算过指纹
        assert state["vl_meta"]["model"] == "mock-vl"
        assert state["vl_meta"]["prompt_tokens"] > 0
        assert "risk_level" in state["vl_result"]

    async def test_reply_never_exceeds_char_limit(self, ctx: AppContext):
        for seed in range(8):
            state, _ = await ctx.process_frame(
                device_id=f"dev-{seed}", frame_jpeg=make_jpeg(seed=seed)
            )
            assert len(state["reply"]["content"]) <= ctx.settings.reply_max_chars


class TestGateShortCircuit:
    async def test_duplicate_frame_returns_noop_and_skips_model(self, ctx: AppContext):
        """闸门驳回必须在模型之前——这是省钱的全部意义所在。"""
        img = make_jpeg(seed=3)
        first, _ = await ctx.process_frame(device_id="dev-dup", frame_jpeg=img)
        assert first["vl_result"] is not None

        second, _ = await ctx.process_frame(device_id="dev-dup", frame_jpeg=img)
        assert second["rejected_by"] == "gate"
        assert second["reject_reason"] == "duplicate"
        assert second["vl_result"] is None  # 模型没被调用
        assert second["reply"]["type"] == "noop"

    async def test_rate_limit_short_circuits(self, settings: Settings):
        settings = settings.model_copy(update={"gate_rate_limit_per_sec": 1.0})
        ctx = AppContext(settings)
        await ctx.startup()
        try:
            a, _ = await ctx.process_frame(device_id="d", frame_jpeg=make_jpeg(seed=1))
            b, _ = await ctx.process_frame(device_id="d", frame_jpeg=make_jpeg(seed=2))
            assert a["rejected_by"] is None
            assert b["reject_reason"] == "rate_limit"
            assert b["vl_result"] is None
        finally:
            await ctx.shutdown()


class TestPreRuleShortCircuit:
    async def test_blurry_frame_gets_template_message_not_model_call(self, ctx: AppContext):
        state, _ = await ctx.process_frame(device_id="dev-blur", frame_jpeg=blurry_jpeg())
        assert state["rejected_by"] == "pre_rules"
        assert state["reject_reason"] == "too_blurry"
        assert state["vl_result"] is None
        assert "模糊" in state["reply"]["content"]

    async def test_garbage_payload_handled_gracefully(self, ctx: AppContext):
        """坏数据在闸门算指纹时就失败，不该崩服务。"""
        state, _ = await ctx.process_frame(device_id="dev-bad", frame_jpeg=b"z" * 4096)
        assert state["rejected_by"] == "gate"
        assert state["reject_reason"] == "decode_failed"
        assert state["reply"] is not None


class TestPostRuleFiltering:
    async def test_banned_word_blocks_reply(self, settings: Settings):
        """mock 会返回"红灯，请等待"，把"红灯"设为违禁词来验证后置拦截。"""
        settings = settings.model_copy(update={"banned_words": ("红灯",)})
        ctx = AppContext(settings)
        await ctx.startup()
        try:
            blocked = None
            for seed in range(12):
                state, _ = await ctx.process_frame(
                    device_id=f"d{seed}", frame_jpeg=make_jpeg(seed=seed)
                )
                if state["rejected_by"] == "post_rules":
                    blocked = state
                    break
            assert blocked is not None, "没有触发到高风险场景，mock 分布有问题"
            assert blocked["reject_reason"] == "banned_word"
            assert blocked["reply"]["content"] == "内容不可展示"
        finally:
            await ctx.shutdown()


class TestModelFailure:
    async def test_backend_failure_yields_fallback_message(self, settings: Settings):
        ctx = AppContext(settings)

        class Broken:
            async def infer(self, image, kb_context):
                raise RuntimeError("upstream down")

            async def close(self):
                pass

        # 替换后端并重建管线，验证注入点真的可替换（§14）
        ctx.backend = Broken()
        from app.graph import build_pipeline

        ctx.pipeline = build_pipeline(
            kv=ctx.kv, kb=ctx.kb, backend=ctx.backend, face=ctx.face,
            settings=ctx.settings, spawn=ctx.background.spawn,
        )
        await ctx.startup()
        try:
            state, _ = await ctx.process_frame(device_id="dev-x", frame_jpeg=make_jpeg(seed=1))
            assert state["error"] is not None
            assert state["reply"]["content"] == "识别失败，请稍后重试"
        finally:
            await ctx.shutdown()

    async def test_hard_deadline_enforced(self, settings: Settings):
        """超过 hard_deadline 直接兜底，不能让眼镜端无限等待（§1）。"""
        settings = settings.model_copy(
            update={"hard_deadline_s": 0.1, "mock_latency_ms": 500, "vl_timeout_s": 10.0}
        )
        ctx = AppContext(settings)
        await ctx.startup()
        try:
            state, elapsed = await ctx.process_frame(
                device_id="dev-slow", frame_jpeg=make_jpeg(seed=1)
            )
            assert state["error"] == "hard_deadline_exceeded"
            assert state["reply"]["content"] == "识别失败，请稍后重试"
            assert elapsed < 0.4  # 确实被截断了，没跑满 500ms
        finally:
            await ctx.shutdown()


class TestRagPrefetch:
    async def test_prefetch_runs_in_background_not_blocking(self, ctx: AppContext):
        """RAG 检索必须在旁路（§5.2）。NullKb 返回空，这里验证任务被登记且能 drain。"""
        await ctx.process_frame(device_id="dev-kb", frame_jpeg=make_jpeg(seed=1))
        drained = await ctx.background.drain(timeout_s=2.0)
        assert drained >= 1

    async def test_context_flows_from_previous_frame(self, settings: Settings):
        """核心机制验证：第 N 帧写入的上下文，被第 N+1 帧读到。"""
        import json

        from app.storage import keys as k

        ctx = AppContext(settings)
        await ctx.startup()
        try:
            device = "dev-flow"
            # 手动种入"上一帧预取结果"
            await ctx.kv.set(k.kb_ctx(device), json.dumps(["红灯含义：禁止通行"]), ttl_s=60)
            state, _ = await ctx.process_frame(device_id=device, frame_jpeg=make_jpeg(seed=1))
            assert state["kb_context"] == ["红灯含义：禁止通行"]
        finally:
            await ctx.shutdown()


class TestConcurrency:
    async def test_many_devices_in_parallel(self, ctx: AppContext):
        """20 台设备并发是首期规模（CLAUDE.md §1）。"""
        results = await asyncio.gather(
            *[
                ctx.process_frame(device_id=f"dev-{i}", frame_jpeg=make_jpeg(seed=i))
                for i in range(20)
            ]
        )
        assert len(results) == 20
        assert all(state["reply"] is not None for state, _ in results)

    async def test_same_device_concurrent_frames_mostly_rejected(self, settings: Settings):
        """同设备并发推图时，限流+去重应该只放行少数——否则成本失控。"""
        settings = settings.model_copy(update={"gate_rate_limit_per_sec": 1.0})
        ctx = AppContext(settings)
        await ctx.startup()
        try:
            results = await asyncio.gather(
                *[
                    ctx.process_frame(device_id="dev-burst", frame_jpeg=make_jpeg(seed=i))
                    for i in range(10)
                ]
            )
            passed = [s for s, _ in results if s["rejected_by"] is None]
            assert len(passed) <= 2, f"限流失效，放行了 {len(passed)} 帧"
        finally:
            await ctx.shutdown()


class TestReadModeEndToEnd:
    """阅读模式全链路。脱敏这条是验收指标「OCR 脱敏规则命中 100%」的证据。"""

    async def test_read_produces_segments(self, ctx: AppContext):
        state, _ = await ctx.process_frame(
            device_id="dev-read", frame_jpeg=make_jpeg(seed=1), trigger="read"
        )
        assert state["error"] is None
        assert state["replies"] is not None
        assert state["vl_result"]["full_text"]

    async def test_every_segment_within_char_limit(self, ctx: AppContext):
        state, _ = await ctx.process_frame(
            device_id="dev-read2", frame_jpeg=make_jpeg(seed=2), trigger="read"
        )
        for reply in state["replies"]:
            assert len(reply["content"]) <= ctx.settings.reply_max_chars

    async def test_id_number_in_full_text_is_redacted(self, settings: Settings):
        """整段读出来的内容最可能含身份证号——必须在回传前脱敏。"""
        from app.inference.backend import VLResponse

        class _IdCardBackend:
            async def infer(self, image_jpeg, kb_context):
                return VLResponse(raw_text="{}", model="mock-vl")

            async def ocr(self, image_jpeg):
                return VLResponse(
                    raw_text="持证人 11010119900307123X\n签发机关 某某分局",
                    model="mock-vl-ocr",
                )

            async def close(self):
                pass

        ctx = AppContext(settings)
        await ctx.startup()
        ctx.backend = _IdCardBackend()
        ctx.pipeline = _rebuild_pipeline(ctx)
        try:
            state, _ = await ctx.process_frame(
                device_id="dev-id", frame_jpeg=make_jpeg(seed=4), trigger="read"
            )
        finally:
            await ctx.shutdown()

        joined = "".join(r["content"] for r in state["replies"])
        assert "11010119900307123X" not in joined
        assert "***" in joined

    async def test_read_rate_limit_is_not_silent(self, settings: Settings):
        """用户主动按了「读这个」，静默 noop 会让人以为设备坏了。"""
        settings = settings.model_copy(update={"gate_read_burst": 1.0})
        ctx = AppContext(settings)
        await ctx.startup()
        try:
            await ctx.process_frame(
                device_id="dev-rl", frame_jpeg=make_jpeg(seed=5), trigger="read"
            )
            state, _ = await ctx.process_frame(
                device_id="dev-rl", frame_jpeg=make_jpeg(seed=6), trigger="read"
            )
        finally:
            await ctx.shutdown()

        assert state["reject_reason"] == "read_rate_limit"
        assert state["reply"]["type"] != "noop"
        assert state["reply"]["content"]


def _rebuild_pipeline(ctx: AppContext):
    from app.graph.pipeline import build_pipeline

    return build_pipeline(
        kv=ctx.kv,
        kb=ctx.kb,
        backend=ctx.backend,
        face=ctx.face,
        settings=ctx.settings,
        spawn=ctx.background.spawn,
    )
