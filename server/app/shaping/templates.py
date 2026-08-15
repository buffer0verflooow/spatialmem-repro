"""结果整形：从结构化输出用模板生成 <=30 字文案（CLAUDE.md §5.3）。

这里全部是模板 + 规则，**不允许再调模型**。任何想在这一层加 LLM 调用的改动，
必须先按 §14 的约定给出延迟实测和成本影响。
"""

from __future__ import annotations

from app.graph.state import Reply
from app.inference.ocr_schema import OcrResult
from app.inference.schema import VLResult
from app.shaping.segment import segment

ELLIPSIS = "…"

# risk_level -> 回传消息类型。alert 会在眼镜端触发震动+语音，voice 只播报，text 只显示
RISK_TO_TYPE = {
    "high": "alert",
    "medium": "voice",
    "low": "text",
    "none": "noop",
}

REJECT_MESSAGES = {
    # 前置规则
    "too_small": "图像数据异常",
    "too_large": "图像过大",
    "decode_failed": "图像无法识别",
    "too_low_resolution": "画面分辨率过低",
    "too_blurry": "画面模糊，请稳一下",
    "face_detected": "画面含人脸，已跳过",
    # 后置规则
    "banned_word": "内容不可展示",
    # 阅读模式
    "read_rate_limit": "阅读太频繁，请稍后再试",
}

ERROR_MESSAGE = "识别失败，请稍后重试"
NOOP_MESSAGE = ""
EMPTY_READ_MESSAGE = "未发现文字"


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars == 1:
        return text[:1]
    return text[: max_chars - 1] + ELLIPSIS


def shape_result(result: VLResult, max_chars: int) -> Reply:
    """正常路径：结构化输出 -> 回传消息。"""
    reply_type = RISK_TO_TYPE.get(result.risk_level, "text")

    content = result.advice.strip()
    if not content:
        content = _compose_fallback_content(result)

    if not content:
        return Reply(type="noop", content=NOOP_MESSAGE)

    # 有话要说但风险等级是 none，降级为纯文字显示而不是静默
    if reply_type == "noop":
        reply_type = "text"

    return Reply(type=reply_type, content=truncate(content, max_chars))


def shape_read_result(result: OcrResult, max_chars: int) -> list[Reply]:
    """阅读模式：把全文切成连续分片，**不截断**。

    分片一次性全下发，固件本地排队播报——服务端不维护播报游标（§4.4）。
    seq 从 1 递增，最后一片 end=True。
    """
    pieces = segment(result.full_text, max_chars)

    if not pieces:
        # 用户主动触发了阅读，静默会让人以为设备坏了
        return [
            Reply(type="text", content=EMPTY_READ_MESSAGE, index=1, total=1, end=True)
        ]

    total = len(pieces)
    return [
        Reply(type="read", content=piece, index=i, total=total, end=(i == total))
        for i, piece in enumerate(pieces, start=1)
    ]


def shape_reject(reason: str | None, max_chars: int) -> Reply:
    """前置/后置规则驳回：给模板兜底文案（对应 CLAUDE.md 流程图的 Y 分支）。"""
    message = REJECT_MESSAGES.get(reason or "", "本帧已跳过")
    return Reply(type="text", content=truncate(message, max_chars))


def shape_error(max_chars: int) -> Reply:
    """模型调用失败/超时：标准化兜底提示。"""
    return Reply(type="text", content=truncate(ERROR_MESSAGE, max_chars))


def shape_noop() -> Reply:
    """闸门驳回：不回传新结果，眼镜端继续显示上一结果（§5.1）。"""
    return Reply(type="noop", content=NOOP_MESSAGE)


def _compose_fallback_content(result: VLResult) -> str:
    """模型没给 advice 时，用 OCR 文字或场景拼一句，避免白屏。"""
    if result.ocr_text.strip():
        return result.ocr_text.strip()
    if result.scene.strip():
        return result.scene.strip()
    return ""
