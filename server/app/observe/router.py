"""POST /v1/observe：客户端空间记忆的结构化观察接口。

开发期设施（本地主机模拟云端）：env=dev 时跳过鉴权；生产接入真云端时再打开
设备鉴权（与 /v1/frame 一致，复用 device_shared_secret）。
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import get_settings
from app.observability import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/v1")


class ObserveRequest(BaseModel):
    frame: str = Field(..., description="JPEG 的 base64，不带 data: 前缀")
    hint: str = Field(default="", description="用户正在问什么（可选上下文）")


class ObserveSupport(BaseModel):
    name: str = ""
    color: str = ""
    location: str = ""
    attributes: str = ""


class ObserveResponse(BaseModel):
    name: str = ""
    color: str = ""
    location: str = ""
    attributes: str = ""
    confidence: float = 0.0
    support: ObserveSupport | None = None


@router.post("/observe", response_model=ObserveResponse)
async def observe(request: Request, body: ObserveRequest) -> ObserveResponse:
    settings = get_settings()
    if settings.env != "dev":
        # 生产：暂未启用（真云端接入时补 X-Device-Token 校验，与 /v1/frame 一致）。
        raise HTTPException(status_code=503, detail="observe not enabled in prod yet")

    ctx = request.app.state.ctx
    try:
        jpeg = base64.b64decode(body.frame, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"base64 解码失败: {exc}") from exc
    if not jpeg:
        raise HTTPException(status_code=400, detail="frame 为空")
    if len(jpeg) > settings.observe_max_frame_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"frame 过大: {len(jpeg)} > {settings.observe_max_frame_bytes}",
        )
    try:
        result = await ctx.observe.observe(jpeg, body.hint)
    except ValueError as exc:
        log.warning("observe_failed", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ObserveResponse(**result)
