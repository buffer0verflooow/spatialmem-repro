"""Redis key 规约（CLAUDE.md §8）。所有 key 只在这里拼，禁止散落在业务代码。"""

from __future__ import annotations


def dev_online(device_id: str) -> str:
    return f"dev:online:{device_id}"


def dev_ratelimit(device_id: str) -> str:
    return f"dev:ratelimit:{device_id}"


def dev_read_ratelimit(device_id: str) -> str:
    """阅读模式的独立令牌桶，与实时帧限流互不挤占。"""
    return f"dev:read_ratelimit:{device_id}"


def dev_lastframe(device_id: str) -> str:
    return f"dev:lastframe:{device_id}"


def kb_ctx(device_id: str) -> str:
    """上一帧预取的 RAG 上下文（§5.2）。"""
    return f"kb_ctx:{device_id}"


def sess_ctx(thread_id: str) -> str:
    return f"sess:ctx:{thread_id}"


def ckpt(thread_id: str) -> str:
    return f"ckpt:{thread_id}"
