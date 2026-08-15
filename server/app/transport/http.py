"""HTTP 备用接口（兼容老设备）+ 运维接口。

HTTP 路径不做背压：调用方是同步等待的，没有"丢弃旧帧"的语义。
限流仍由闸门统一负责。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.config import reload_settings
from app.observability import get_logger
from app.observability.metrics import auth_failure_total
from app.observability.security_log import log_auth_failure
from app.runtime import AppContext
from app.transport.auth import verify
from app.transport.security import require_admin_key
from app.transport.signature import verify_signature
from app.transport.wire import FrameMessage, ReplyMessage

log = get_logger(__name__)
router = APIRouter(prefix="/v1")


class FrameRequest(FrameMessage):
    device_id: str


@router.post("/frame", response_model=ReplyMessage)
async def post_frame(
    request: Request,
    body: FrameRequest,
    x_device_token: str = Header(default=""),
    x_signature: str = Header(default=""),
    x_timestamp: int = Header(default=0),
) -> ReplyMessage:
    ctx: AppContext = request.app.state.ctx

    # 基础设备鉴权
    if not verify(body.device_id, x_device_token, ctx.settings.device_shared_secret):
        auth_failure_total.labels(type="http").inc()
        ip = request.client.host if request.client else ""
        log_auth_failure("http", body.device_id, ip)
        raise HTTPException(status_code=401, detail="unauthorized")

    # 请求签名校验（携带了签名 header 时启用）
    if x_signature and x_timestamp:
        raw_body = await request.body()
        if not verify_signature(
            body.device_id,
            x_timestamp,
            raw_body,
            ctx.settings.device_shared_secret,
            x_signature,
        ):
            auth_failure_total.labels(type="http").inc()
            ip = request.client.host if request.client else ""
            log_auth_failure("http", body.device_id, ip)
            raise HTTPException(status_code=401, detail="invalid signature or expired")

    try:
        payload = body.decode()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    state, elapsed = await ctx.process_frame(
        device_id=body.device_id,
        frame_jpeg=payload,
        seq=body.seq,
        trigger=body.trigger,
    )
    reply = state.get("reply") or {"type": "error", "content": "no_reply"}
    replies = state.get("replies")

    return ReplyMessage(
        type=reply["type"],
        content=reply["content"],
        seq=body.seq,
        latency_ms=int(elapsed * 1000),
        index=reply.get("index", 1),
        total=reply.get("total", 1),
        end=reply.get("end", True),
        # HTTP 单响应：阅读模式一次给全，调用方不需要再轮询
        segments=[r["content"] for r in replies] if replies else None,
    )


# ---------------- 运维接口 ----------------

admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])


class KbReloadRequest(BaseModel):
    persist_dir: str | None = None


@admin.post("/kb/reload")
async def kb_reload(request: Request, body: KbReloadRequest) -> dict:
    """知识库原子切换（CLAUDE.md §4.5）。入库由离线脚本完成，这里只切目录。"""
    ctx: AppContext = request.app.state.ctx
    target = body.persist_dir or ctx.settings.kb_dir
    try:
        count = await ctx.kb.reload(target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"kb reload failed: {exc}") from exc
    return {"persist_dir": target, "chunks": count, "ready": ctx.kb.ready}


@admin.post("/config/reload")
async def config_reload() -> dict:
    """热更新阈值。注意：只对每次请求读 settings 的代码生效；

    已注入到节点闭包里的 settings 引用不会变——闸门/规则阈值需要重启生效。
    W7 调参时用重启，不要依赖这个接口。
    """
    settings = reload_settings()
    return {
        "env": settings.env,
        "gate_rate_limit_per_sec": settings.gate_rate_limit_per_sec,
        "gate_phash_dup_distance": settings.gate_phash_dup_distance,
        "gate_min_interval_s": settings.gate_min_interval_s,
        "note": "闸门/规则阈值需重启进程生效，见本接口 docstring",
    }


@admin.get("/devices")
async def list_devices() -> dict:
    from app.transport.ws import registry

    return {"online": len(registry), "device_ids": registry.device_ids}
