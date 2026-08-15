"""阅读模式分片切分（CLAUDE.md §5.1 阅读模式）。

眼镜端单条消息仍受 reply_max_chars 约束，整段文字必须切成多片连续播报。
切点必须落在语义边界上——硬切会把「38 元」拆成「38」「元」，播报出来是错的。
"""

from __future__ import annotations

import re

import pytest

from app.shaping.segment import segment


def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


class TestEmpty:
    def test_empty_text_yields_no_segments(self):
        assert segment("", 30) == []

    def test_whitespace_only_yields_no_segments(self):
        assert segment("   \n\n  ", 30) == []


class TestShortText:
    def test_text_within_limit_stays_one_segment(self):
        assert segment("安全出口在右前方", 30) == ["安全出口在右前方"]

    def test_text_exactly_at_limit_stays_one_segment(self):
        text = "一" * 30
        assert segment(text, 30) == [text]


class TestBoundaries:
    def test_splits_on_newline_first(self):
        text = "凉菜类\n口水鸡 38 元"
        assert segment(text, 30) == ["凉菜类", "口水鸡 38 元"]

    def test_splits_on_sentence_punctuation(self):
        text = "本店招牌是水煮鱼。人均消费 60 元。"
        assert segment(text, 15) == ["本店招牌是水煮鱼。", "人均消费 60 元。"]

    def test_splits_on_comma_when_no_sentence_end(self):
        text = "口水鸡 38 元，夫妻肺片 42 元，蒜泥白肉 45 元"
        segments = segment(text, 15)
        assert all(len(s) <= 15 for s in segments)
        # 关键：价格不能被拆开
        for s in segments:
            assert not s.endswith("38")
            assert not s.endswith("42")

    def test_hard_splits_when_no_punctuation_available(self):
        text = "一" * 70
        segments = segment(text, 30)
        assert [len(s) for s in segments] == [30, 30, 10]


class TestInvariants:
    @pytest.mark.parametrize(
        "text",
        [
            "川菜馆菜单\n凉菜类：口水鸡 38 元，夫妻肺片 42 元。\n热菜类：水煮鱼 68 元。",
            "一" * 200,
            "短句。",
            "无标点的一长串文字" * 12,
        ],
    )
    def test_every_segment_within_limit(self, text):
        assert all(len(s) <= 30 for s in segment(text, 30))

    @pytest.mark.parametrize(
        "text",
        [
            "川菜馆菜单\n凉菜类：口水鸡 38 元，夫妻肺片 42 元。\n热菜类：水煮鱼 68 元。",
            "一" * 200,
            "无标点的一长串文字" * 12,
        ],
    )
    def test_no_content_is_lost(self, text):
        assert _strip_ws("".join(segment(text, 30))) == _strip_ws(text)

    def test_no_empty_segments(self):
        text = "第一句。\n\n\n第二句。"
        assert all(s.strip() for s in segment(text, 30))


class TestGuards:
    def test_non_positive_limit_returns_whole_text(self):
        """max_chars 配错时不能死循环，退化为整段返回。"""
        assert segment("一二三", 0) == ["一二三"]
