"""阅读模式分片切分。纯函数，无 IO、无模型调用。

眼镜端单条消息仍受 reply_max_chars 约束，所以整段文字要切成多片连续播报。
切点必须落在语义边界上：硬切会把「38 元」拆成「38」「元」，播报出来是错的。

优先级：换行 > 句末标点 > 句中标点 > 空格 > 硬切。
"""

from __future__ import annotations

# 句末：切在这里最自然，一片就是一个完整意群
SENTENCE_END = "。！？!?；;"
# 句中：次选，至少不会切断词或数字+量词
CLAUSE_END = "，、,：:）)】」』》"


def segment(text: str, max_chars: int) -> list[str]:
    """把整段文字切成每片 <= max_chars 的列表。

    max_chars <= 0 时退化为整段返回——配置写错不能让服务死循环。
    """
    if not text.strip():
        return []
    if max_chars <= 0:
        return [text]

    out: list[str] = []
    for paragraph in text.split("\n"):
        stripped = paragraph.strip()
        if stripped:
            out.extend(_split_paragraph(stripped, max_chars))
    return out


def _split_paragraph(paragraph: str, max_chars: int) -> list[str]:
    out: list[str] = []
    rest = paragraph
    while len(rest) > max_chars:
        cut = _find_cut(rest, max_chars)
        piece = rest[:cut].strip()
        if piece:
            out.append(piece)
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def _find_cut(text: str, max_chars: int) -> int:
    """在前 max_chars 个字符内找最靠后的语义边界，返回切点下标（不含）。"""
    window = text[:max_chars]

    for marks in (SENTENCE_END, CLAUSE_END):
        idx = max((window.rfind(m) for m in marks), default=-1)
        if idx >= 0:
            return idx + 1  # 标点留在片尾，不丢内容

    idx = window.rfind(" ")
    if idx > 0:
        return idx + 1

    return max_chars  # 无边界可用，只能硬切
