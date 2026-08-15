"""Agent HTTP 路由。

提供物品识别的 HTTP API 和交互式对话接口。
"""

from __future__ import annotations

import base64
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent.runner import AgentRunner
from app.observability import get_logger
from app.transport.security import require_agent_key

log = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["agent"], dependencies=[Depends(require_agent_key)])


# ---------------- 请求/响应模型 ----------------


class RecognizeRequest(BaseModel):
    """物品识别请求。"""

    image: str = Field(..., description="图片 base64 编码，不带 data URI 前缀")
    detail_level: Literal["brief", "detailed"] = Field(
        default="detailed", description="识别详细程度"
    )
    question: str | None = Field(default=None, description="可选的附加问题")


class RecognizedObject(BaseModel):
    """识别到的物品。"""

    name: str
    name_en: str | None = None
    position: str | None = None
    count: int = 1
    description: str | None = None


class RecognizeResponse(BaseModel):
    """物品识别响应。"""

    text: str = Field(..., description="完整的识别结果文本")
    objects: list[RecognizedObject] | None = Field(
        default=None, description="结构化物品列表"
    )
    latency_ms: int = Field(..., description="处理耗时（毫秒）")


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str = Field(..., description="用户消息")
    image: str | None = Field(default=None, description="可选的图片 base64 编码")
    session_id: str | None = Field(
        default=None, description="会话 ID，用于多轮对话；不传则创建新会话"
    )


class ChatResponse(BaseModel):
    """对话响应。"""

    text: str = Field(..., description="Agent 回复")
    session_id: str = Field(..., description="会话 ID")
    tool_calls: list[str] = Field(
        default_factory=list, description="本次对话中调用的工具"
    )
    objects: list[RecognizedObject] | None = Field(
        default=None, description="如果涉及物品识别，返回结构化结果"
    )
    latency_ms: int = Field(..., description="处理耗时（毫秒）")


# ---------------- 路由 ----------------


@router.post("/recognize", response_model=RecognizeResponse)
async def recognize(request: Request, body: RecognizeRequest) -> RecognizeResponse:
    """单次物品识别。

    上传一张图片，返回详细的物品识别结果。

    示例请求：
    ```json
    {
        "image": "<base64 编码的图片>",
        "detail_level": "detailed",
        "question": "这是什么品牌的手机？"
    }
    ```
    """
    ctx = request.app.state.ctx
    agent: AgentRunner = ctx.agent

    try:
        image_bytes = base64.b64decode(body.image, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"图片 base64 解码失败: {exc}") from exc

    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片过大（上限 10MB）")

    try:
        response = await agent.recognize(
            image_bytes,
            detail_level=body.detail_level,
            question=body.question,
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("recognize_failed")
        raise HTTPException(status_code=500, detail=f"识别失败: {exc}") from exc

    objects = None
    if response.objects:
        objects = [RecognizedObject(**obj) for obj in response.objects]

    return RecognizeResponse(
        text=response.text,
        objects=objects,
        latency_ms=response.latency_ms,
    )


@router.post("/agent/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """交互式对话。

    支持与 Agent 进行多轮对话，可以上传图片进行物品识别和问答。

    示例请求：
    ```json
    {
        "message": "这张图片里有什么？",
        "image": "<base64 编码的图片，可选>",
        "session_id": "可选，用于多轮对话"
    }
    ```

    多轮对话示例：
    1. 第一轮：发送图片 + "识别图中的物品"
    2. 第二轮（带 session_id）："第一个物品是什么材质的？"
    """
    ctx = request.app.state.ctx
    agent: AgentRunner = ctx.agent

    image_bytes = None
    if body.image:
        try:
            image_bytes = base64.b64decode(body.image, validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"图片 base64 解码失败: {exc}"
            ) from exc

    try:
        response = await agent.chat(
            body.message,
            image_bytes=image_bytes,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("agent_chat_failed", session_id=body.session_id)
        raise HTTPException(status_code=500, detail=f"对话失败: {exc}") from exc

    objects = None
    if response.objects:
        objects = [RecognizedObject(**obj) for obj in response.objects]

    return ChatResponse(
        text=response.text,
        session_id=response.session_id or "",
        tool_calls=response.tool_calls_made,
        objects=objects,
        latency_ms=response.latency_ms,
    )


@router.post("/agent/session/reset")
async def reset_session(request: Request, session_id: str) -> dict:
    """重置会话。清除指定会话的消息历史。"""
    ctx = request.app.state.ctx
    agent: AgentRunner = ctx.agent

    if session_id in agent._sessions:
        agent._sessions[session_id].reset()
        return {"status": "ok", "session_id": session_id}
    return {"status": "not_found", "session_id": session_id}
