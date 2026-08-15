"""结构化输出解析。目标失败率 <1%（CLAUDE.md §12）。

三级降级：严格 JSON -> 提取花括号片段 -> 正则抓 advice。
每一级失败都打点，失败率进指标看板。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.inference.ocr_schema import OcrResult
from app.inference.schema import VLResult
from app.observability import get_logger
from app.observability.metrics import parse_failure_total

log = get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_ADVICE_RE = re.compile(r'"advice"\s*:\s*"([^"]{1,128})"')


def parse(raw: str) -> tuple[VLResult, bool]:
    """返回 (结果, 是否走了降级路径)。永不抛异常。"""
    if not raw or not raw.strip():
        parse_failure_total.labels(stage="json").inc()
        return VLResult(), True

    text = raw.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    # 一级：严格 JSON
    result = _try_json(text)
    if result is not None:
        return result, False

    parse_failure_total.labels(stage="json").inc()

    # 二级：从噪声中抠出第一个花括号对象
    match = _OBJECT_RE.search(text)
    if match:
        result = _try_json(match.group(0))
        if result is not None:
            log.info("vl_parse_recovered_by_brace_extract")
            return result, True

    parse_failure_total.labels(stage="schema").inc()

    # 三级：正则只抓 advice，保证用户至少收到一句话
    advice = _ADVICE_RE.search(raw)
    if advice:
        log.warning("vl_parse_recovered_by_regex", advice=advice.group(1))
        return VLResult(advice=advice.group(1)), True

    parse_failure_total.labels(stage="fallback_regex").inc()
    log.warning("vl_parse_failed", raw_head=raw[:200])
    return VLResult(), True


_OCR_TEXT_KEYS = ("full_text", "text", "content")

# 提示词约定的哨兵值（见 prompt.OCR_SYSTEM_PROMPT）。它是「没有文字」的信号，
# 不是内容——不归一化的话会被当正文播报，shape 层的空分支永远走不到。
_NO_TEXT_SENTINEL = "未发现文字"
_SENTINEL_TRIM = " \t\r\n。.！!，,、"


def parse_ocr(raw: str, max_chars: int) -> OcrResult:
    """阅读模式解析。永不抛异常。

    方向和 parse() 相反：qwen-vl-ocr 返回**纯文本才是常态**，JSON 是例外。
    所以认不出的内容一律原样保留——阅读模式下丢内容比格式不整齐更糟。

    max_chars 是成本与体验保护：不截断会产生几百条分片消息。
    """
    if not raw or not raw.strip():
        return OcrResult()

    text = raw.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    extracted = _extract_from_json(text)
    if extracted is not None:
        text = extracted.strip()

    # 只在整段输出就是哨兵本身时归一化。用 strip 后全等判断而不是子串匹配，
    # 否则「检测报告…未发现文字，其余合格」这类真实内容会被误杀。
    if text.strip(_SENTINEL_TRIM) == _NO_TEXT_SENTINEL:
        return OcrResult()

    if max_chars > 0 and len(text) > max_chars:
        log.info("ocr_text_truncated", original_len=len(text), limit=max_chars)
        text = text[:max_chars]

    return OcrResult(full_text=text)


def _extract_from_json(text: str) -> str | None:
    """模型偶尔会包一层 JSON。认得出就拆，认不出返回 None 走原样。"""
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in _OCR_TEXT_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _try_json(text: str) -> VLResult | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return VLResult.model_validate(data)
    except ValidationError as exc:
        log.debug("vl_schema_invalid", error=str(exc))
        return None
