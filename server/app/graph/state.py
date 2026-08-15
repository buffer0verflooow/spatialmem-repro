"""管线状态（CLAUDE.md §8）。

关键约定：State 内传 bytes 而非 base64 字符串，避免 33% 体积膨胀和多次编解码。
仅接入层边界做 base64 转换。
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

Trigger = Literal["auto", "manual", "read"]
ReplyType = Literal["text", "voice", "alert", "noop", "error", "read"]


class Reply(TypedDict):
    type: ReplyType
    content: str
    # 仅阅读模式分片使用；缺省即「单片且已结束」。
    # index 是分片位置，与帧序号 seq 是两回事——共用会让固件把 N 片当成 N 个帧。
    index: NotRequired[int]
    total: NotRequired[int]
    end: NotRequired[bool]


class FrameState(TypedDict, total=False):
    # ---- 输入（接入层填充）----
    thread_id: str  # f"{device_id}:{session_seq}"
    device_id: str
    frame_jpeg: bytes
    timestamp: float
    trigger: Trigger
    seq: int

    # ---- 过程 ----
    phash: str
    hash_distance: int | None
    since_last_call_s: float | None
    kb_context: list[str]  # 上一帧预取（§5.2）
    vl_result: dict[str, Any] | None
    vl_meta: dict[str, Any]  # model / tokens / latency_ms / second_call

    # ---- 输出 ----
    reply: Reply | None
    # 阅读模式的全部分片；普通帧为 None，transport 据此走单条路径
    replies: list[Reply] | None
    rejected_by: str | None  # 驳回节点名，None 表示通过
    reject_reason: str | None
    error: str | None


def new_state(
    *,
    device_id: str,
    frame_jpeg: bytes,
    timestamp: float,
    seq: int = 0,
    trigger: Trigger = "auto",
    session_seq: int = 0,
) -> FrameState:
    return FrameState(
        thread_id=f"{device_id}:{session_seq}",
        device_id=device_id,
        frame_jpeg=frame_jpeg,
        timestamp=timestamp,
        trigger=trigger,
        seq=seq,
        phash="",
        hash_distance=None,
        since_last_call_s=None,
        kb_context=[],
        vl_result=None,
        vl_meta={},
        reply=None,
        replies=None,
        rejected_by=None,
        reject_reason=None,
        error=None,
    )


def is_blocked(state: FrameState) -> bool:
    return bool(state.get("rejected_by")) or bool(state.get("error"))
