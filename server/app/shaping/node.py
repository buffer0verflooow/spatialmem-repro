"""整形节点 + 兜底节点。全部是纯模板逻辑，无 IO、无模型调用。"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.gate.node import NODE as GATE_NODE
from app.inference.ocr_schema import OcrResult
from app.inference.schema import VLResult
from app.observability.metrics import timed
from app.shaping.templates import (
    shape_error,
    shape_noop,
    shape_read_result,
    shape_reject,
    shape_result,
)

NODE = "shape"
FALLBACK_NODE = "fallback"


def make_shape_node(settings: Settings):
    async def shape_node(state: dict[str, Any]) -> dict[str, Any]:
        with timed(NODE):
            raw = state.get("vl_result") or {}

            if state.get("trigger") == "read":
                replies = shape_read_result(
                    OcrResult.model_validate(raw), settings.reply_max_chars
                )
                # reply 指向第一片：HTTP 单响应路径和日志仍读这个字段
                return {"reply": replies[0], "replies": replies}

            result = VLResult.model_validate(raw)
            return {"reply": shape_result(result, settings.reply_max_chars)}

    return shape_node


def make_fallback_node(settings: Settings):
    """三条兜底路径：闸门驳回(noop) / 规则驳回(模板文案) / 模型失败(标准提示)。"""

    async def fallback_node(state: dict[str, Any]) -> dict[str, Any]:
        with timed(FALLBACK_NODE):
            rejected_by = state.get("rejected_by")

            # 阅读模式是用户主动发起的，任何驳回都必须有可见反馈。
            # 静默 noop 会让人以为设备坏了，反复按——正好放大限流问题。
            if rejected_by == GATE_NODE and state.get("trigger") != "read":
                return {"reply": shape_noop()}
            if rejected_by:
                return {"reply": shape_reject(state.get("reject_reason"), settings.reply_max_chars)}
            return {"reply": shape_error(settings.reply_max_chars)}

    return fallback_node
