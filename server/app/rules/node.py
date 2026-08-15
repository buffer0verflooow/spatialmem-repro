"""规则层的两个管线节点：前置校验、后置过滤。"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.observability.metrics import reject_total, timed
from app.rules.face import FaceDetector
from app.rules.post import sanitize_vl_result
from app.rules.pre import run_static_checks

PRE_NODE = "pre_rules"
POST_NODE = "post_rules"


def make_pre_rules_node(face: FaceDetector, settings: Settings):
    async def pre_rules_node(state: dict[str, Any]) -> dict[str, Any]:
        with timed(PRE_NODE):
            image: bytes = state["frame_jpeg"]

            static = run_static_checks(image)
            if not static.ok:
                reject_total.labels(node=PRE_NODE, reason=static.reason).inc()
                return {"rejected_by": PRE_NODE, "reject_reason": static.reason}

            # 阅读模式不做人脸驳回：菜单、说明书、文件上常印着人像或证件照，
            # 按实时帧的规则会整帧被驳回，功能直接不可用。隐私改由后置的
            # full_text 脱敏兜住（CLAUDE.md §4.7 本就是「不做图像级判断」）。
            if settings.face_detect_enabled and state.get("trigger") != "read":
                boxes = await face.detect(image)
                if boxes:
                    reject_total.labels(node=PRE_NODE, reason="face_detected").inc()
                    return {"rejected_by": PRE_NODE, "reject_reason": "face_detected"}

            return {"rejected_by": None, "reject_reason": None}

    return pre_rules_node


def make_post_rules_node(settings: Settings):
    async def post_rules_node(state: dict[str, Any]) -> dict[str, Any]:
        with timed(POST_NODE):
            result = state.get("vl_result")
            if not result:
                return {"rejected_by": None, "reject_reason": None}

            cleaned, check = sanitize_vl_result(
                result,
                redact_patterns=settings.redact_patterns,
                banned=settings.banned_words,
            )
            if not check.ok:
                reject_total.labels(node=POST_NODE, reason=check.reason).inc()
                return {
                    "vl_result": cleaned,
                    "rejected_by": POST_NODE,
                    "reject_reason": check.reason,
                }
            return {"vl_result": cleaned, "rejected_by": None, "reject_reason": None}

    return post_rules_node
