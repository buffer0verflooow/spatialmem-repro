"""Prompt 注入防护：检测恶意注入 + 过滤输出泄露。

覆盖两类威胁：
1. 输入侧：用户通过文本/图片描述注入恶意指令（如 "ignore previous instructions"）
2. 输出侧：模型输出中包含系统提示词片段（提示词泄露）
"""

from __future__ import annotations

import re

# 常见注入模式（英文 + 中文）
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"repeat\s+(the\s+|your\s+)?instructions", re.IGNORECASE),
    re.compile(r"output\s+(your\s+|the\s+)?system\s+(prompt|message)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+|previous\s+)?(rules|instructions|prompts)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+|your\s+)?(previous\s+)?(rules|instructions|prompts)", re.IGNORECASE),
    re.compile(r"pretend\s+(you\s+are|to\s+be)", re.IGNORECASE),
    re.compile(r"act\s+as\s+if\s+you\s+(are|have\s+no)", re.IGNORECASE),
    re.compile(r"忽略.*(之前|以上|所有).*(指令|规则|提示词)", re.IGNORECASE),
    re.compile(r"你现在是", re.IGNORECASE),
    re.compile(r"输出.*(系统|你的).*(提示词|指令|prompt)", re.IGNORECASE),
]

# 片段长度：用于检测输出中是否包含系统提示词的子串
_FRAGMENT_LEN = 40


def detect_injection(text: str) -> bool:
    """检测文本中是否包含 Prompt 注入模式。

    Args:
        text: 用户输入的文本

    Returns:
        True 表示检测到注入尝试
    """
    if not text:
        return False
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def filter_output(text: str, system_prompt: str) -> str:
    """过滤输出中可能泄露的系统提示词片段。

    将系统提示词按固定长度切片，检查输出是否包含这些片段。
    命中则替换为占位符。

    Args:
        text: 模型输出文本
        system_prompt: 系统提示词原文

    Returns:
        过滤后的安全文本
    """
    if not text or not system_prompt:
        return text

    cleaned = text
    # 对系统提示词做滑窗切片，检测连续片段
    for i in range(0, len(system_prompt) - _FRAGMENT_LEN + 1, _FRAGMENT_LEN):
        fragment = system_prompt[i : i + _FRAGMENT_LEN]
        if fragment in cleaned:
            cleaned = cleaned.replace(fragment, "[内容已过滤]")

    return cleaned
