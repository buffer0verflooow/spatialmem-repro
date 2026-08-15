"""阅读模式的 OCR 结果解析。

与 VLResult 的解析方向相反：qwen-vl-ocr 返回的**纯文本才是常态**，
JSON 是例外。所以这里不做「JSON 失败才降级」，而是先认纯文本。
"""

from __future__ import annotations

from app.inference.ocr_schema import OcrResult
from app.inference.parser import parse_ocr

LIMIT = 2000


class TestPlainText:
    def test_plain_text_becomes_full_text(self):
        assert parse_ocr("口水鸡 38 元", LIMIT).full_text == "口水鸡 38 元"

    def test_surrounding_whitespace_is_stripped(self):
        assert parse_ocr("  \n菜单\n  ", LIMIT).full_text == "菜单"

    def test_newlines_inside_are_preserved(self):
        """换行是分片的最强边界，不能在解析阶段丢掉。"""
        assert parse_ocr("凉菜类\n口水鸡 38 元", LIMIT).full_text == "凉菜类\n口水鸡 38 元"

    def test_empty_input_yields_empty_result(self):
        assert parse_ocr("", LIMIT).full_text == ""

    def test_whitespace_only_input_yields_empty_result(self):
        assert parse_ocr("   \n ", LIMIT).full_text == ""


class TestFences:
    def test_markdown_fence_is_removed(self):
        assert parse_ocr("```\n菜单内容\n```", LIMIT).full_text == "菜单内容"

    def test_json_tagged_fence_is_removed(self):
        raw = '```json\n{"full_text": "菜单内容"}\n```'
        assert parse_ocr(raw, LIMIT).full_text == "菜单内容"


class TestJsonForm:
    def test_json_with_full_text_is_extracted(self):
        assert parse_ocr('{"full_text": "水煮鱼 68 元"}', LIMIT).full_text == "水煮鱼 68 元"

    def test_json_with_text_key_is_extracted(self):
        assert parse_ocr('{"text": "水煮鱼 68 元"}', LIMIT).full_text == "水煮鱼 68 元"

    def test_json_without_known_key_falls_back_to_raw(self):
        """认不出的 JSON 宁可原样播报，也不要丢内容。"""
        raw = '{"unexpected": "值"}'
        assert parse_ocr(raw, LIMIT).full_text == raw

    def test_json_array_falls_back_to_raw(self):
        raw = '["a", "b"]'
        assert parse_ocr(raw, LIMIT).full_text == raw


class TestNoTextSentinel:
    """提示词约定「图中没有文字时输出：未发现文字」。

    它是哨兵不是内容——不归一化的话 full_text 非空，shape 层会当正文播报，
    回传 type=read 而不是 type=text，空分支永远走不到。
    """

    def test_sentinel_normalized_to_empty(self):
        assert parse_ocr("未发现文字", LIMIT).is_empty()

    def test_sentinel_with_trailing_punctuation(self):
        assert parse_ocr("未发现文字。", LIMIT).is_empty()

    def test_sentinel_with_surrounding_whitespace(self):
        assert parse_ocr("  未发现文字 \n", LIMIT).is_empty()

    def test_real_text_containing_the_phrase_is_kept(self):
        """整段文档里恰好出现这几个字时不能误杀。"""
        raw = "检测报告\n第三项：未发现文字，其余合格。"
        assert not parse_ocr(raw, LIMIT).is_empty()


class TestLimit:
    def test_overlong_text_is_truncated(self):
        """上限是成本与体验保护：不截断会产生几百条分片消息。"""
        result = parse_ocr("一" * 5000, LIMIT)
        assert len(result.full_text) == LIMIT

    def test_text_at_limit_is_kept_whole(self):
        result = parse_ocr("一" * LIMIT, LIMIT)
        assert len(result.full_text) == LIMIT


class TestSchema:
    def test_defaults_to_empty(self):
        assert OcrResult().full_text == ""

    def test_is_empty_reports_blank_text(self):
        assert OcrResult(full_text="  ").is_empty()
        assert not OcrResult(full_text="菜单").is_empty()


# --------------------------------------------------------------------------
# 推理层路由：trigger=read 走 qwen-vl-ocr，不走 qwen-vl-plus
# --------------------------------------------------------------------------

from app.inference.backend import MockBackend, VLResponse  # noqa: E402
from app.inference.node import make_infer_node  # noqa: E402
from app.kb.store import NullKb  # noqa: E402
from app.storage import MemoryKV  # noqa: E402
from tests.conftest import make_jpeg  # noqa: E402


class _RecordingBackend:
    """分别记录两条路径的调用次数，用来证明路由确实分叉了。"""

    def __init__(self, *, ocr_text: str = "凉菜类\n口水鸡 38 元") -> None:
        self.infer_calls = 0
        self.ocr_calls = 0
        self._ocr_text = ocr_text

    async def infer(self, image_jpeg, kb_context):
        self.infer_calls += 1
        return VLResponse(raw_text='{"advice":"前方右转"}', model="qwen-vl-plus")

    async def ocr(self, image_jpeg):
        self.ocr_calls += 1
        return VLResponse(raw_text=self._ocr_text, model="qwen-vl-ocr")

    async def close(self):
        pass


class _FailingOcrBackend(_RecordingBackend):
    async def ocr(self, image_jpeg):
        self.ocr_calls += 1
        raise RuntimeError("ocr boom")


def _make_node(backend, settings, spawned: list[str]):
    def spawn(coro, name: str) -> None:
        spawned.append(name)
        coro.close()  # 测试里不真跑旁路任务

    return make_infer_node(
        backend=backend, kb=NullKb(), kv=MemoryKV(), settings=settings, spawn=spawn
    )


def _read_state():
    return {
        "device_id": "d1",
        "frame_jpeg": make_jpeg(seed=5),
        "timestamp": 0.0,
        "trigger": "read",
    }


class TestMockBackendOcr:
    async def test_deterministic(self):
        backend = MockBackend()
        img = make_jpeg(seed=7)
        assert (await backend.ocr(img)).raw_text == (await backend.ocr(img)).raw_text

    async def test_returns_multiline_text(self):
        """必须能产出多行，否则分片逻辑在 mock 下测不到换行边界。"""
        resp = await MockBackend().ocr(make_jpeg(seed=7))
        assert "\n" in resp.raw_text

    async def test_reports_tokens(self):
        resp = await MockBackend().ocr(make_jpeg(seed=1))
        assert resp.prompt_tokens > 0
        assert resp.completion_tokens > 0


class TestInferRouting:
    async def test_read_trigger_calls_ocr_not_infer(self, settings):
        backend = _RecordingBackend()
        node = _make_node(backend, settings, [])

        await node(_read_state())

        assert backend.ocr_calls == 1
        assert backend.infer_calls == 0

    async def test_auto_trigger_still_calls_infer(self, settings):
        backend = _RecordingBackend()
        node = _make_node(backend, settings, [])

        await node({**_read_state(), "trigger": "auto"})

        assert backend.infer_calls == 1
        assert backend.ocr_calls == 0

    async def test_read_result_carries_full_text(self, settings):
        node = _make_node(_RecordingBackend(), settings, [])

        out = await node(_read_state())

        assert out["vl_result"]["full_text"] == "凉菜类\n口水鸡 38 元"
        assert out["error"] is None

    async def test_read_does_not_spawn_kb_prefetch(self, settings):
        """RAG 上下文对整段 OCR 没有价值，白花一次检索。"""
        spawned: list[str] = []
        node = _make_node(_RecordingBackend(), settings, spawned)

        await node(_read_state())

        assert "kb_prefetch" not in spawned

    async def test_read_never_triggers_second_call(self, settings):
        """§5.3 的复核例外只针对 risk_level=high，阅读模式没有这个概念。"""
        settings = settings.model_copy(update={"second_call_enabled": True})
        backend = _RecordingBackend()
        node = _make_node(backend, settings, [])

        out = await node(_read_state())

        assert backend.ocr_calls == 1
        assert out["vl_meta"]["second_call"] is False

    async def test_read_records_ocr_model_in_meta(self, settings):
        node = _make_node(_RecordingBackend(), settings, [])
        out = await node(_read_state())
        assert out["vl_meta"]["model"] == "qwen-vl-ocr"

    async def test_ocr_failure_surfaces_as_error(self, settings):
        settings = settings.model_copy(update={"ocr_retries": 0})
        node = _make_node(_FailingOcrBackend(), settings, [])

        out = await node(_read_state())

        assert out["error"] is not None
        assert "ocr boom" in out["error"]

    async def test_ocr_respects_its_own_retry_budget(self, settings):
        settings = settings.model_copy(update={"ocr_retries": 1})
        backend = _FailingOcrBackend()
        node = _make_node(backend, settings, [])

        await node(_read_state())

        assert backend.ocr_calls == 2  # retries + 1
