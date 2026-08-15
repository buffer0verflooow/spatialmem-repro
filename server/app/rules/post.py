"""后置规则：敏感词拦截 + OCR 输出脱敏（CLAUDE.md §4.7）。

涉密文档不做图像级判断，改为对模型 OCR 输出做正则脱敏——
这条路径的验收指标是「脱敏规则命中率 100%」，可用单测完全覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.observability.metrics import redaction_total
from app.observability.security_log import log_redaction

MASK = "***"

# 自由文本字段，全部走脱敏；objects / keywords 是受控词表，不动
FREE_TEXT_FIELDS = ("ocr_text", "advice", "scene", "full_text")
# 模式索引 → 可读名称（按 redact_patterns 元组顺序）
_PATTERN_NAMES = ("id_card", "bank_card", "phone", "email", "plate")


@dataclass(slots=True)
class PostCheck:
    ok: bool
    reason: str = ""
    hit: str = ""


@lru_cache(maxsize=32)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


def redact(text: str, patterns: tuple[str, ...]) -> str:
    """按顺序替换敏感模式。长模式应排在前面，避免被短模式先吃掉。

    每次命中时递增 redaction_total 指标并记录安全日志。
    """
    if not text:
        return text
    out = text
    for idx, pattern in enumerate(_compiled(patterns)):
        matches = pattern.findall(out)
        if matches:
            name = _PATTERN_NAMES[idx] if idx < len(_PATTERN_NAMES) else f"pattern_{idx}"
            for _ in matches:
                redaction_total.labels(pattern=name).inc()
            log_redaction(name)
            out = pattern.sub(MASK, out)
    return out


def check_banned_words(text: str, banned: tuple[str, ...]) -> PostCheck:
    if not text or not banned:
        return PostCheck(True)
    lowered = text.lower()
    for word in banned:
        if word and word.lower() in lowered:
            return PostCheck(False, "banned_word", word)
    return PostCheck(True)


def sanitize_vl_result(
    result: dict, *, redact_patterns: tuple[str, ...], banned: tuple[str, ...]
) -> tuple[dict, PostCheck]:
    """对模型输出中的自由文本字段做脱敏与拦截。

    处理 ocr_text / advice / scene / full_text 四个自由文本字段；
    objects / keywords 是受控词表，不做脱敏以免破坏检索关键词。

    full_text 只在阅读模式出现，但它恰恰是最危险的一个——整段读出来的内容
    最可能包含身份证号、病历、工牌。实时帧路径没这个字段，缺失是正常的。
    """
    cleaned = dict(result)
    for field in FREE_TEXT_FIELDS:
        value = cleaned.get(field)
        if isinstance(value, str):
            cleaned[field] = redact(value, redact_patterns)

    joined = " ".join(str(cleaned.get(f, "")) for f in FREE_TEXT_FIELDS)
    return cleaned, check_banned_words(joined, banned)
