"""眼镜端协议（对应 docs/api.md）。

W3 定稿后发固件团队，W6 联调。改动必须同步 docs/api.md 与固件版本约定。
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_B64_LEN = 6 * 1024 * 1024  # base64 后的上限，约 4.5MB 原图


class FrameMessage(BaseModel):
    """眼镜端 -> 服务端"""

    type: Literal["frame"] = "frame"
    seq: int = 0
    ts: float | None = None
    trigger: Literal["auto", "manual", "read"] = "auto"
    image: str = Field(description="JPEG 的 base64，不带 data URI 前缀")

    @field_validator("image")
    @classmethod
    def _check_len(cls, v: str) -> str:
        if not v:
            raise ValueError("image 不能为空")
        if len(v) > MAX_B64_LEN:
            raise ValueError(f"image 超长: {len(v)} > {MAX_B64_LEN}")
        return v

    def decode(self) -> bytes:
        """base64 -> bytes。只在接入层边界做一次转换（CLAUDE.md §8）。"""
        payload = self.image
        if payload.startswith("data:"):
            _, _, payload = payload.partition(",")
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"base64 解码失败: {exc}") from exc


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"


class ReplyMessage(BaseModel):
    """服务端 -> 眼镜端

    type=noop 表示本帧被闸门驳回，眼镜端应**继续显示上一结果**，不要清屏。
    这是最容易被固件实现错的一点（CLAUDE.md §5.1）。

    type=read 是阅读模式分片：同一次请求会连续下发 total 片，seq 从 1 递增，
    最后一片 end=true。固件收到后本地排队顺序播报；用户中途中断只需清空
    本地队列，**不要回报服务端**——服务端不维护播报游标（§4.4）。
    """

    type: Literal["text", "voice", "alert", "noop", "error", "read"]
    content: str = ""
    seq: int = 0  # 帧序号，原样回显；同一次阅读的所有分片共用同一个 seq
    latency_ms: int = 0
    # 非阅读模式恒为 (1, 1, True)，老固件忽略这三个字段也能正确工作
    index: int = 1  # 分片位置，1..total
    total: int = 1
    end: bool = True
    # 仅 HTTP 阅读模式：HTTP 是单响应，没有连续下发语义，一次给全
    segments: list[str] | None = None


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    content: str
    seq: int = 0
