from __future__ import annotations

from app.rules.node import make_pre_rules_node
from app.rules.post import MASK, check_banned_words, redact, sanitize_vl_result
from app.rules.pre import (
    blur_score,
    check_decodable,
    check_dimensions,
    check_payload_size,
    run_static_checks,
)
from tests.conftest import blurry_jpeg, make_jpeg


class TestPreRules:
    def test_valid_frame_passes_all(self):
        assert run_static_checks(make_jpeg(seed=3)).ok

    def test_tiny_payload_rejected(self):
        assert check_payload_size(b"x" * 10).reason == "too_small"

    def test_oversized_payload_rejected(self):
        assert check_payload_size(b"x" * (5 * 1024 * 1024)).reason == "too_large"

    def test_garbage_not_decodable(self):
        assert check_decodable(b"y" * 2048).reason == "decode_failed"

    def test_low_resolution_rejected(self):
        assert check_dimensions(make_jpeg(width=32, height=32, seed=1)).reason == (
            "too_low_resolution"
        )

    def test_blurry_frame_rejected(self):
        """回归测试：FIND_EDGES 的边框 artifact 曾让纯色图方差虚高到 ~290，
        导致模糊检测完全失效。裁边后必须回到接近 0。"""
        assert blur_score(blurry_jpeg()) < 5.0
        assert run_static_checks(blurry_jpeg()).reason == "too_blurry"

    def test_sharp_frame_far_above_threshold(self):
        assert blur_score(make_jpeg(seed=1)) > 500.0

    def test_checks_run_cheapest_first(self):
        """尺寸检查最便宜，坏数据应该在这一步就短路，不进解码。"""
        assert run_static_checks(b"x" * 10).reason == "too_small"


class TestRedact:
    PATTERNS = (r"\d{17}[\dXx]", r"\d{16,19}", r"1[3-9]\d{9}")

    def test_id_card_masked(self):
        out = redact("身份证 11010519491231002X 已登记", self.PATTERNS)
        assert "11010519491231002X" not in out
        assert MASK in out

    def test_phone_masked(self):
        assert redact("联系 13812345678", self.PATTERNS) == f"联系 {MASK}"

    def test_bank_card_masked(self):
        out = redact("卡号 6222021234567890123", self.PATTERNS)
        assert "6222021234567890123" not in out

    def test_clean_text_untouched(self):
        text = "前方红灯，请等待"
        assert redact(text, self.PATTERNS) == text

    def test_empty_text_safe(self):
        assert redact("", self.PATTERNS) == ""

    def test_longer_pattern_wins(self):
        """身份证模式排在银行卡前面，18 位号码不应被 16-19 位模式先切碎。"""
        out = redact("11010519491231002X", self.PATTERNS)
        assert out == MASK


class TestBannedWords:
    def test_hit_is_rejected(self):
        check = check_banned_words("这里有违规内容", ("违规",))
        assert not check.ok
        assert check.hit == "违规"

    def test_case_insensitive(self):
        assert not check_banned_words("Contains BadWord here", ("badword",)).ok

    def test_empty_list_passes_everything(self):
        assert check_banned_words("anything at all", ()).ok


class TestSanitizeVlResult:
    def test_free_text_fields_redacted(self):
        result = {
            "scene": "办公室",
            "ocr_text": "工号 13812345678",
            "advice": "联系 13800001111",
            "keywords": ["工号"],
            "objects": ["badge"],
            "risk_level": "low",
        }
        cleaned, check = sanitize_vl_result(
            result, redact_patterns=TestRedact.PATTERNS, banned=()
        )
        assert check.ok
        assert "13812345678" not in cleaned["ocr_text"]
        assert "13800001111" not in cleaned["advice"]

    def test_keywords_not_redacted(self):
        """keywords 是受控词表，脱敏会破坏 RAG 检索质量，必须原样保留。"""
        result = {"keywords": ["13812345678"], "ocr_text": "", "advice": "", "scene": ""}
        cleaned, _ = sanitize_vl_result(
            result, redact_patterns=TestRedact.PATTERNS, banned=()
        )
        assert cleaned["keywords"] == ["13812345678"]

    def test_banned_word_in_advice_blocks(self):
        result = {"advice": "包含违规词", "ocr_text": "", "scene": "", "keywords": []}
        _, check = sanitize_vl_result(result, redact_patterns=(), banned=("违规",))
        assert not check.ok
        assert check.reason == "banned_word"

    def test_original_dict_not_mutated(self):
        result = {"advice": "13812345678", "ocr_text": "", "scene": "", "keywords": []}
        sanitize_vl_result(result, redact_patterns=TestRedact.PATTERNS, banned=())
        assert result["advice"] == "13812345678"


class _AlwaysDetectsFace:
    async def detect(self, image_bytes: bytes) -> list[tuple[int, int, int, int]]:
        return [(0, 0, 10, 10)]


class TestReadModeFaceExemption:
    """阅读模式关闭人脸驳回。

    菜单、说明书、文件上常印着人像或证件照，按实时帧的规则会整帧被驳回，
    功能直接不可用。隐私由后置的 full_text 脱敏兜住，不靠图像级驳回。
    """

    async def test_read_mode_keeps_frame_containing_face(self, settings):
        settings = settings.model_copy(update={"face_detect_enabled": True})
        node = make_pre_rules_node(_AlwaysDetectsFace(), settings)

        out = await node({"frame_jpeg": make_jpeg(seed=3), "trigger": "read"})
        assert out["rejected_by"] is None

    async def test_realtime_frame_still_rejects_face(self, settings):
        settings = settings.model_copy(update={"face_detect_enabled": True})
        node = make_pre_rules_node(_AlwaysDetectsFace(), settings)

        out = await node({"frame_jpeg": make_jpeg(seed=3), "trigger": "auto"})
        assert out["reject_reason"] == "face_detected"

    async def test_read_mode_still_rejects_blurry_frame(self, settings):
        """模糊图 OCR 必然出错，早退比调模型便宜——这条豁免不了。"""
        settings = settings.model_copy(update={"face_detect_enabled": True})
        node = make_pre_rules_node(_AlwaysDetectsFace(), settings)

        out = await node({"frame_jpeg": blurry_jpeg(), "trigger": "read"})
        assert out["reject_reason"] == "too_blurry"


class TestSanitizeOcrFullText:
    """阅读模式最可能读到身份证号、病历、工牌——脱敏必须覆盖 full_text。"""

    def test_full_text_is_redacted(self):
        result = {"full_text": "持证人 11010119900307123X 已登记", "advice": ""}
        cleaned, check = sanitize_vl_result(
            result, redact_patterns=(r"\d{17}[\dXx]",), banned=()
        )
        assert "11010119900307123X" not in cleaned["full_text"]
        assert MASK in cleaned["full_text"]
        assert check.ok

    def test_banned_word_in_full_text_blocks(self):
        result = {"full_text": "内部资料 违规词 请勿外传", "advice": ""}
        _, check = sanitize_vl_result(
            result, redact_patterns=(), banned=("违规词",)
        )
        assert not check.ok
        assert check.reason == "banned_word"

    def test_missing_full_text_is_harmless(self):
        """实时帧路径没有 full_text 字段，不能因此报错。"""
        result = {"ocr_text": "出口", "advice": "前方右转", "scene": "走廊"}
        cleaned, check = sanitize_vl_result(result, redact_patterns=(), banned=())
        assert check.ok
        assert "full_text" not in cleaned
