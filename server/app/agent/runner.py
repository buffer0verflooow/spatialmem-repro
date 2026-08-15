"""Agent 运行器。

实现基于 Qwen-VL function calling 的物品识别 Agent 核心循环。
支持单轮识别和多轮对话。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.prompt_guard import detect_injection, filter_output
from app.agent.prompts import (
    AGENT_SYSTEM_PROMPT,
    build_recognize_prompt,
)
from app.agent.tools import TOOLS, execute_tool
from app.config import Settings
from app.inference.image import normalize
from app.kb.store import KbStore
from app.observability import get_logger
from app.observability.metrics import (
    agent_tool_audit_total,
    injection_attempt_total,
    rate_limit_total,
)
from app.observability.security_log import (
    log_injection_attempt,
    log_rate_limit,
    log_tool_audit,
)

log = get_logger(__name__)


@dataclass
class AgentMessage:
    """对话消息。"""

    role: str  # system / user / assistant / tool
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class AgentSession:
    """对话会话，维护消息历史。"""

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    image_b64: str | None = None
    created_at: float = field(default_factory=time.time)
    request_timestamps: deque = field(default_factory=lambda: deque(maxlen=200))

    def add_message(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)

    def reset(self) -> None:
        self.messages.clear()
        self.image_b64 = None

    def record_request(self) -> None:
        """记录当前请求时间戳（用于滑动窗口限频）。"""
        self.request_timestamps.append(time.time())

    def count_requests_in_window(self, window_s: float = 60.0) -> int:
        """统计最近 window_s 秒内的请求数。"""
        cutoff = time.time() - window_s
        # 清除过期记录
        while self.request_timestamps and self.request_timestamps[0] < cutoff:
            self.request_timestamps.popleft()
        return len(self.request_timestamps)


@dataclass
class AgentResponse:
    """Agent 响应结果。"""

    text: str
    tool_calls_made: list[str] = field(default_factory=list)
    objects: list[dict[str, Any]] | None = None
    session_id: str | None = None
    latency_ms: int = 0


class AgentRunner:
    """Agent 运行器，管理与模型的交互循环。"""

    def __init__(self, settings: Settings, kb: KbStore | None = None) -> None:
        self.settings = settings
        self.kb = kb or _NullKb()
        self._client: httpx.AsyncClient | None = None
        self._sessions: dict[str, AgentSession] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            api_key = self.settings.dashscope_api_key
            if not api_key:
                raise ValueError(
                    "Agent 需要 DASHSCOPE_API_KEY，请在 .env 中设置"
                )
            self._client = httpx.AsyncClient(
                base_url=self.settings.dashscope_base_url.rstrip("/"),
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def get_or_create_session(self, session_id: str | None = None) -> AgentSession:
        """获取或创建会话。"""
        if session_id is None:
            session_id = f"agent_{uuid.uuid4().hex[:12]}"
        if session_id not in self._sessions:
            self._sessions[session_id] = AgentSession(session_id=session_id)
        return self._sessions[session_id]

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        detail_level: str = "detailed",
        question: str | None = None,
    ) -> AgentResponse:
        """单次物品识别（无状态）。

        Args:
            image_bytes: 图片原始字节
            detail_level: 识别详细程度 (brief/detailed)
            question: 可选的附加问题

        Returns:
            AgentResponse 包含识别结果
        """
        start = time.perf_counter()

        # Prompt 注入检测
        if self.settings.agent_injection_guard_enabled:
            for text in (question or "",):
                if detect_injection(text):
                    injection_attempt_total.inc()
                    log_injection_attempt()
                    raise ValueError("检测到不安全的输入内容")

        # 图像预处理
        image, _ = normalize(
            image_bytes,
            max_edge=self.settings.image_max_edge,
            quality=self.settings.image_jpeg_quality,
        )
        image_b64 = base64.b64encode(image).decode()

        # 构建消息
        messages = self._build_recognize_messages(image_b64, detail_level, question)

        # 调用模型（不使用 function calling，直接识别）
        response = await self._call_model(messages)

        # 输出过滤
        if self.settings.agent_injection_guard_enabled:
            response = filter_output(response, AGENT_SYSTEM_PROMPT)

        # 输出长度截断
        max_chars = self.settings.agent_output_max_chars
        if len(response) > max_chars:
            response = response[:max_chars] + "..."

        latency_ms = int((time.perf_counter() - start) * 1000)

        return AgentResponse(
            text=response,
            objects=self._parse_objects(response),
            latency_ms=latency_ms,
        )

    async def chat(
        self,
        message: str,
        *,
        image_bytes: bytes | None = None,
        session_id: str | None = None,
    ) -> AgentResponse:
        """交互式对话。

        Args:
            message: 用户消息
            image_bytes: 可选的图片
            session_id: 会话 ID（用于多轮对话）

        Returns:
            AgentResponse 包含回复
        """
        start = time.perf_counter()
        session = self.get_or_create_session(session_id)

        # 会话级频率限制
        session.record_request()
        limit = self.settings.agent_rate_limit_per_session
        if session.count_requests_in_window() > limit:
            rate_limit_total.labels(scope="session").inc()
            log_rate_limit("session", session.session_id)
            raise ValueError(f"会话请求过于频繁（上限 {limit} 次/分钟）")

        # Prompt 注入检测
        if self.settings.agent_injection_guard_enabled:
            if detect_injection(message):
                injection_attempt_total.inc()
                log_injection_attempt(session.session_id)
                raise ValueError("检测到不安全的输入内容")

        # 处理图片
        image_b64 = None
        if image_bytes:
            image, _ = normalize(
                image_bytes,
                max_edge=self.settings.image_max_edge,
                quality=self.settings.image_jpeg_quality,
            )
            image_b64 = base64.b64encode(image).decode()
            session.image_b64 = image_b64

        # 构建用户消息
        user_msg = self._build_user_message(message, image_b64)
        session.add_message(user_msg)

        # Agent 循环（支持工具调用）
        tool_calls_made: list[str] = []
        max_rounds = self.settings.agent_max_tool_rounds

        for round_num in range(max_rounds):
            messages = self._build_chat_messages(session)
            response_data = await self._call_model_with_tools(messages)

            # 检查是否有工具调用
            tool_calls = response_data.get("tool_calls")
            if not tool_calls:
                # 直接回复，结束循环
                text = response_data.get("content", "")

                # 输出过滤 + 长度截断
                if self.settings.agent_injection_guard_enabled:
                    text = filter_output(text, AGENT_SYSTEM_PROMPT)
                max_chars = self.settings.agent_output_max_chars
                if len(text) > max_chars:
                    text = text[:max_chars] + "..."

                session.add_message({"role": "assistant", "content": text})
                latency_ms = int((time.perf_counter() - start) * 1000)
                return AgentResponse(
                    text=text,
                    tool_calls_made=tool_calls_made,
                    objects=self._parse_objects(text),
                    session_id=session.session_id,
                    latency_ms=latency_ms,
                )

            # 执行工具调用
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response_data.get("content"),
                "tool_calls": tool_calls,
            }
            session.add_message(assistant_msg)

            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                tool_call_id = tc.get("id", "")

                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                tool_calls_made.append(name)
                result = await execute_tool(
                    name, args, session.image_b64, self.kb
                )

                # 审计日志 + 指标
                agent_tool_audit_total.labels(tool=name, outcome="ok").inc()
                log_tool_audit(name, "ok", session.session_id)

                # 添加工具结果消息
                session.add_message({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                })

            log.info(
                "agent_tool_round_complete",
                round=round_num + 1,
                tools=[tc.get("function", {}).get("name") for tc in tool_calls],
            )

        # 超过最大轮次，强制生成最终回复
        messages = self._build_chat_messages(session)
        final_text = await self._call_model(messages)

        # 输出过滤 + 长度截断
        if self.settings.agent_injection_guard_enabled:
            final_text = filter_output(final_text, AGENT_SYSTEM_PROMPT)
        max_chars = self.settings.agent_output_max_chars
        if len(final_text) > max_chars:
            final_text = final_text[:max_chars] + "..."

        session.add_message({"role": "assistant", "content": final_text})
        latency_ms = int((time.perf_counter() - start) * 1000)

        return AgentResponse(
            text=final_text,
            tool_calls_made=tool_calls_made,
            session_id=session.session_id,
            latency_ms=latency_ms,
        )

    def _build_recognize_messages(
        self, image_b64: str, detail_level: str, question: str | None
    ) -> list[dict[str, Any]]:
        """构建识别请求的消息列表。"""
        prompt = build_recognize_prompt(detail_level)
        if question:
            prompt = f"{prompt}\n\n用户附加问题：{question}"

        return [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        ]

    def _build_user_message(
        self, message: str, image_b64: str | None
    ) -> dict[str, Any]:
        """构建用户消息。"""
        if image_b64:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": message},
                ],
            }
        return {"role": "user", "content": message}

    def _build_chat_messages(
        self, session: AgentSession
    ) -> list[dict[str, Any]]:
        """构建对话消息列表（包含系统提示词和历史）。"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT}
        ]
        messages.extend(session.messages)
        return messages

    async def _call_model(self, messages: list[dict[str, Any]]) -> str:
        """直接调用模型（不使用工具）。"""
        client = await self._get_client()
        body = {
            "model": self.settings.agent_model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.3,
        }

        resp = await client.post("/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()

        return data["choices"][0]["message"]["content"] or ""

    async def _call_model_with_tools(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """调用模型并启用工具。返回包含 content 和 tool_calls 的 dict。"""
        client = await self._get_client()
        body = {
            "model": self.settings.agent_model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_tokens": 2000,
            "temperature": 0.3,
        }

        resp = await client.post("/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]

        return {
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls"),
        }

    def _parse_objects(self, text: str) -> list[dict[str, Any]]:
        """尝试从文本中提取物品列表（简单启发式解析）。"""
        objects: list[dict[str, Any]] = []

        # 尝试匹配 "**物品名 (English Name)**" 格式
        import re

        pattern = r"\*\*([^*]+?)\s*\(([^)]+)\)\*\*"
        matches = re.findall(pattern, text)
        for cn_name, en_name in matches:
            objects.append({
                "name": cn_name.strip(),
                "name_en": en_name.strip(),
            })

        return objects if objects else None


class _NullKb:
    """空知识库占位。"""

    ready = False

    async def search(self, query: str, top_k: int, min_score: float) -> list[str]:
        return []
