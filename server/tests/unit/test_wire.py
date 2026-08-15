"""眼镜端协议的阅读模式扩展（docs/api.md，W3 定稿）。

分片一次性全下发，服务端不维护播报游标——固件本地排队播报，
中断由固件清空本地队列处理。这是守住 CLAUDE.md §4.4 无状态原则的前提。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.graph.state import new_state
from app.transport.wire import FrameMessage, ReplyMessage

IMAGE = "aGVsbG8="  # base64("hello")


class TestFrameTrigger:
    def test_accepts_read_trigger(self):
        msg = FrameMessage(image=IMAGE, trigger="read")
        assert msg.trigger == "read"

    def test_still_accepts_auto_and_manual(self):
        assert FrameMessage(image=IMAGE).trigger == "auto"
        assert FrameMessage(image=IMAGE, trigger="manual").trigger == "manual"

    def test_rejects_unknown_trigger(self):
        with pytest.raises(ValidationError):
            FrameMessage(image=IMAGE, trigger="scan")


class TestReplyFraming:
    def test_accepts_read_type(self):
        assert ReplyMessage(type="read", content="凉菜类").type == "read"

    def test_carries_segment_position(self):
        msg = ReplyMessage(
            type="read", content="凉菜类", seq=42, index=2, total=8, end=False
        )
        assert (msg.index, msg.total, msg.end) == (2, 8, False)

    def test_seq_stays_the_frame_number_not_the_segment_number(self):
        """seq 是帧序号，固件靠它匹配请求-响应。同一次阅读的所有分片 seq 相同，
        分片位置走 index——两者共用一个字段会让固件把 8 片当成 8 个帧。"""
        msg = ReplyMessage(type="read", content="凉菜类", seq=42, index=3, total=8)
        assert msg.seq == 42

    def test_defaults_keep_existing_firmware_working(self):
        """现有固件不认 index/total/end，默认值必须表示「单片且已结束」。"""
        msg = ReplyMessage(type="text", content="红灯，请等待")
        assert msg.index == 1
        assert msg.total == 1
        assert msg.end is True

    def test_existing_reply_types_unaffected(self):
        for t in ("text", "voice", "alert", "noop", "error"):
            assert ReplyMessage(type=t, content="x").type == t


class TestState:
    def test_new_state_accepts_read_trigger(self):
        state = new_state(
            device_id="dev-1", frame_jpeg=b"x", timestamp=0.0, trigger="read"
        )
        assert state["trigger"] == "read"

    def test_new_state_has_no_segments_by_default(self):
        """普通实时帧不产生分片，replies 保持 None 让 transport 走单条路径。"""
        state = new_state(device_id="dev-1", frame_jpeg=b"x", timestamp=0.0)
        assert state["replies"] is None
