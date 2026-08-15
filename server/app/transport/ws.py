"""WebSocket 接入层。

每条连接一个 LatestOnlySlot + 一个消费任务：接收循环永不阻塞，
新帧直接覆盖未处理的旧帧（CLAUDE.md §5.1 背压）。
这样即使模型慢到 3 秒，眼镜端按 5 帧/秒推图也不会把服务端拖垮。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.gate import LatestOnlySlot
from app.observability import get_logger
from app.observability.metrics import devices_online
from app.runtime import AppContext
from app.storage import keys as k
from app.transport.auth import verify
from app.transport.wire import ErrorMessage, FrameMessage, PongMessage, ReplyMessage

log = get_logger(__name__)
router = APIRouter()

WS_CLOSE_UNAUTHORIZED = 4401


@dataclass(slots=True)
class PendingFrame:
    payload: bytes
    seq: int
    trigger: str


class ConnectionRegistry:
    """在线连接表。用于 /admin 查询和优雅关停。"""

    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}

    def add(self, device_id: str, ws: WebSocket) -> None:
        self._conns[device_id] = ws
        devices_online.set(len(self._conns))

    def remove(self, device_id: str) -> None:
        self._conns.pop(device_id, None)
        devices_online.set(len(self._conns))

    @property
    def device_ids(self) -> list[str]:
        return sorted(self._conns)

    def __len__(self) -> int:
        return len(self._conns)


registry = ConnectionRegistry()


@router.websocket("/ws/glass/{device_id}")
async def glass_ws(
    websocket: WebSocket,
    device_id: str,
    token: str = Query(default=""),
) -> None:
    ctx: AppContext = websocket.app.state.ctx

    if not verify(device_id, token, ctx.settings.device_shared_secret):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="unauthorized")
        log.warning("ws_auth_failed", device_id=device_id)
        return

    await websocket.accept()
    registry.add(device_id, websocket)
    # 会话标识：同一设备重连后视为新会话，避免和上次的上下文串味
    session_seq = int(time.time())
    slot: LatestOnlySlot[PendingFrame] = LatestOnlySlot()
    consumer = asyncio.create_task(
        _consume(websocket, ctx, device_id, slot, session_seq),
        name=f"ws_consume:{device_id}",
    )
    log.info("ws_connected", device_id=device_id, session_seq=session_seq)

    try:
        await _receive_loop(websocket, ctx, device_id, slot)
    except WebSocketDisconnect:
        log.info("ws_disconnected", device_id=device_id)
    except Exception as exc:
        log.warning("ws_receive_error", device_id=device_id, error=str(exc))
    finally:
        slot.close()
        consumer.cancel()
        registry.remove(device_id)
        await ctx.kv.delete(k.dev_online(device_id))


async def _receive_loop(
    websocket: WebSocket,
    ctx: AppContext,
    device_id: str,
    slot: LatestOnlySlot[PendingFrame],
) -> None:
    while True:
        raw = await websocket.receive_json()

        msg_type = raw.get("type") if isinstance(raw, dict) else None
        if msg_type == "ping":
            await ctx.kv.set(
                k.dev_online(device_id), "1", ttl_s=ctx.settings.ws_heartbeat_timeout_s
            )
            await websocket.send_json(PongMessage().model_dump())
            continue

        try:
            frame = FrameMessage.model_validate(raw)
            payload = frame.decode()
        except (ValidationError, ValueError) as exc:
            await websocket.send_json(
                ErrorMessage(content=f"bad_frame: {exc}"[:180]).model_dump()
            )
            continue

        await ctx.kv.set(
            k.dev_online(device_id), "1", ttl_s=ctx.settings.ws_heartbeat_timeout_s
        )
        # slot 满时丢弃旧帧，接收循环永不阻塞
        slot.put(PendingFrame(payload=payload, seq=frame.seq, trigger=frame.trigger))


async def _consume(
    websocket: WebSocket,
    ctx: AppContext,
    device_id: str,
    slot: LatestOnlySlot[PendingFrame],
    session_seq: int,
) -> None:
    while True:
        item = await slot.get()
        if item is None:  # slot 已关闭
            return
        try:
            state, elapsed = await ctx.process_frame(
                device_id=device_id,
                frame_jpeg=item.payload,
                seq=item.seq,
                trigger=item.trigger,
                session_seq=session_seq,
            )
            reply = state.get("reply") or {"type": "error", "content": "no_reply"}
            # 阅读模式：分片一次性连续发完，固件本地排队播报。
            # 服务端不维护播报游标，中断由固件清空本地队列处理（§4.4）。
            outgoing = state.get("replies") or [reply]
            latency_ms = int(elapsed * 1000)

            for part in outgoing:
                await websocket.send_json(
                    ReplyMessage(
                        type=part["type"],
                        content=part["content"],
                        seq=item.seq,  # 帧序号，所有分片一致
                        latency_ms=latency_ms,
                        index=part.get("index", 1),
                        total=part.get("total", 1),
                        end=part.get("end", True),
                    ).model_dump()
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ws_consume_error", device_id=device_id, error=str(exc))
            return
