from __future__ import annotations

from app.inference.schema import VLResult
from app.shaping.templates import (
    ERROR_MESSAGE,
    shape_error,
    shape_noop,
    shape_reject,
    shape_result,
    truncate,
)

MAX = 30


class TestTruncate:
    def test_short_text_untouched(self):
        assert truncate("红灯，请等待", MAX) == "红灯，请等待"

    def test_long_text_gets_ellipsis(self):
        out = truncate("很长的一句话" * 10, MAX)
        assert len(out) == MAX
        assert out.endswith("…")

    def test_exact_boundary_untouched(self):
        text = "字" * MAX
        assert truncate(text, MAX) == text

    def test_chinese_counted_by_character_not_byte(self):
        """30 字是字符数不是字节数——按字节算会把中文腰斩成 10 个字。"""
        assert len(truncate("字" * 100, 30)) == 30


class TestShapeResult:
    def test_high_risk_becomes_alert(self):
        reply = shape_result(VLResult(risk_level="high", advice="红灯，请等待"), MAX)
        assert reply["type"] == "alert"
        assert reply["content"] == "红灯，请等待"

    def test_medium_becomes_voice(self):
        assert shape_result(VLResult(risk_level="medium", advice="注意台阶"), MAX)["type"] == (
            "voice"
        )

    def test_low_becomes_text(self):
        assert shape_result(VLResult(risk_level="low", advice="前方是书店"), MAX)["type"] == "text"

    def test_none_risk_with_no_advice_is_noop(self):
        """没什么值得说的就别说——每一条无意义播报都在消耗用户注意力。"""
        reply = shape_result(VLResult(risk_level="none", advice=""), MAX)
        assert reply["type"] == "noop"
        assert reply["content"] == ""

    def test_none_risk_with_advice_downgrades_to_text(self):
        """有话要说但不涉安全，显示而不播报，别静默丢掉。"""
        reply = shape_result(VLResult(risk_level="none", advice="星巴克"), MAX)
        assert reply["type"] == "text"

    def test_falls_back_to_ocr_when_advice_missing(self):
        reply = shape_result(VLResult(risk_level="low", advice="", ocr_text="安全出口"), MAX)
        assert reply["content"] == "安全出口"

    def test_falls_back_to_scene_when_ocr_also_missing(self):
        reply = shape_result(VLResult(risk_level="low", advice="", scene="地铁站"), MAX)
        assert reply["content"] == "地铁站"

    def test_long_advice_truncated(self):
        reply = shape_result(VLResult(risk_level="high", advice="注意" * 40), MAX)
        assert len(reply["content"]) == MAX


class TestFallbacks:
    def test_gate_reject_is_silent_noop(self):
        """闸门驳回不回传新结果，眼镜端继续显示上一结果（§5.1）。"""
        reply = shape_noop()
        assert reply["type"] == "noop"
        assert reply["content"] == ""

    def test_blurry_reject_has_actionable_message(self):
        reply = shape_reject("too_blurry", MAX)
        assert reply["type"] == "text"
        assert "模糊" in reply["content"]

    def test_banned_word_reject_message(self):
        assert shape_reject("banned_word", MAX)["content"] == "内容不可展示"

    def test_unknown_reason_has_generic_message(self):
        assert shape_reject("something_new", MAX)["content"] == "本帧已跳过"

    def test_none_reason_handled(self):
        assert shape_reject(None, MAX)["content"] == "本帧已跳过"

    def test_model_error_message(self):
        assert shape_error(MAX)["content"] == ERROR_MESSAGE

    def test_all_fallbacks_respect_max_chars(self):
        for reply in (shape_error(5), shape_reject("too_blurry", 5), shape_noop()):
            assert len(reply["content"]) <= 5


# --------------------------------------------------------------------------
# 阅读模式：分片而非截断
# --------------------------------------------------------------------------

from app.config import Settings  # noqa: E402
from app.gate.node import REASON_READ_RATE_LIMIT  # noqa: E402
from app.inference.ocr_schema import OcrResult  # noqa: E402
from app.shaping.node import make_fallback_node, make_shape_node  # noqa: E402
from app.shaping.templates import EMPTY_READ_MESSAGE, shape_read_result  # noqa: E402

MENU = "川菜馆菜单\n凉菜类：口水鸡 38 元，夫妻肺片 42 元。\n热菜类：水煮鱼 68 元。"


class TestShapeReadResult:
    def test_produces_multiple_segments(self):
        replies = shape_read_result(OcrResult(full_text=MENU), 15)
        assert len(replies) > 1

    def test_every_segment_typed_read(self):
        for r in shape_read_result(OcrResult(full_text=MENU), 15):
            assert r["type"] == "read"

    def test_segments_numbered_from_one(self):
        replies = shape_read_result(OcrResult(full_text=MENU), 15)
        assert [r["index"] for r in replies] == list(range(1, len(replies) + 1))

    def test_total_is_consistent_across_segments(self):
        replies = shape_read_result(OcrResult(full_text=MENU), 15)
        assert all(r["total"] == len(replies) for r in replies)

    def test_only_last_segment_marks_end(self):
        replies = shape_read_result(OcrResult(full_text=MENU), 15)
        assert replies[-1]["end"] is True
        assert all(r["end"] is False for r in replies[:-1])

    def test_segments_respect_char_limit(self):
        for r in shape_read_result(OcrResult(full_text=MENU), 15):
            assert len(r["content"]) <= 15

    def test_no_ellipsis_truncation(self):
        """阅读模式要读全文，不是截断——出现省略号说明走错了路径。"""
        replies = shape_read_result(OcrResult(full_text=MENU), 15)
        assert all("…" not in r["content"] for r in replies)

    def test_empty_text_still_answers_user(self):
        """用户主动触发了阅读，静默是坏体验——必须明确说没找到文字。"""
        replies = shape_read_result(OcrResult(full_text=""), 30)
        assert len(replies) == 1
        assert replies[0]["content"] == EMPTY_READ_MESSAGE
        assert replies[0]["end"] is True
        assert replies[0]["index"] == 1


class TestShapeNodeRouting:
    async def test_read_state_yields_segment_list(self, settings: Settings):
        node = make_shape_node(settings)
        out = await node({"trigger": "read", "vl_result": {"full_text": MENU}})

        assert out["replies"] is not None
        assert len(out["replies"]) > 1

    async def test_read_state_sets_reply_to_first_segment(self, settings: Settings):
        """HTTP 单响应路径和日志仍读 reply，指向第一片保持兼容。"""
        node = make_shape_node(settings)
        out = await node({"trigger": "read", "vl_result": {"full_text": MENU}})

        assert out["reply"] == out["replies"][0]

    async def test_realtime_state_has_no_segments(self, settings: Settings):
        node = make_shape_node(settings)
        out = await node(
            {"trigger": "auto", "vl_result": {"advice": "红灯，请等待", "risk_level": "high"}}
        )

        assert out.get("replies") is None
        assert out["reply"]["type"] == "alert"


class TestReadFallback:
    async def test_read_rate_limit_gets_explicit_message(self, settings: Settings):
        """用户主动按了「读这个」，静默 noop 会让人以为设备坏了。"""
        node = make_fallback_node(settings)
        out = await node(
            {"trigger": "read", "rejected_by": "gate", "reject_reason": REASON_READ_RATE_LIMIT}
        )

        assert out["reply"]["type"] != "noop"
        assert out["reply"]["content"]

    async def test_realtime_gate_reject_stays_noop(self, settings: Settings):
        """实时帧被闸门驳回仍必须是 noop——眼镜端继续显示上一结果（§5.1）。"""
        node = make_fallback_node(settings)
        out = await node(
            {"trigger": "auto", "rejected_by": "gate", "reject_reason": "duplicate"}
        )

        assert out["reply"]["type"] == "noop"
