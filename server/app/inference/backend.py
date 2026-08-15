"""模型后端。mock 用于开发与测试（无需 API key），dashscope 走真实调用。

DashScope 用 OpenAI 兼容端点 + httpx，而不是官方 SDK：SDK 是同步的，
在 asyncio 服务里会阻塞事件循环。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.inference.prompt import (
    OCR_SYSTEM_PROMPT,
    OCR_USER_PROMPT,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from app.observability import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class VLResponse:
    raw_text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    extra: dict = field(default_factory=dict)


class VLTimeout(Exception):
    pass


class VLCallFailed(Exception):
    pass


@runtime_checkable
class VLBackend(Protocol):
    async def infer(self, image_jpeg: bytes, kb_context: list[str]) -> VLResponse: ...
    async def ocr(self, image_jpeg: bytes) -> VLResponse: ...
    async def close(self) -> None: ...


class MockBackend:
    """确定性假响应：同一张图永远得到同一结果，便于写断言。

    latency_ms 可配，用来在本地复现 §6 的延迟预算（默认 0，测试跑得快）。
    """

    MODEL = "mock-vl"
    OCR_MODEL = "mock-vl-ocr"

    # 多行是必须的：分片逻辑以换行为最强边界，单行文本测不到这条路径
    _DOCUMENTS = (
        "川菜馆菜单\n凉菜类：口水鸡 38 元，夫妻肺片 42 元。\n热菜类：水煮鱼 68 元。",
        "安全须知\n一、请勿倚靠车门。\n二、紧急情况请按下红色按钮。",
        "营业时间\n周一至周五 09:00-18:00\n周六周日 10:00-16:00",
        "未发现文字",
    )

    _SCENES = (
        (
            "城市人行道",
            ["crosswalk", "traffic_light"],
            ["人行横道", "红灯"],
            "high",
            "红灯，请等待",
        ),
        ("室内走廊", ["door", "sign"], ["安全出口"], "low", "前方右转是出口"),
        ("楼梯口", ["stairs", "handrail"], ["台阶"], "medium", "注意台阶"),
        ("普通街景", ["building", "tree"], ["街道"], "none", ""),
    )

    def __init__(self, latency_ms: int = 0) -> None:
        self._latency_ms = latency_ms

    async def infer(self, image_jpeg: bytes, kb_context: list[str]) -> VLResponse:
        start = time.perf_counter()
        if self._latency_ms > 0:
            import asyncio

            await asyncio.sleep(self._latency_ms / 1000)

        digest = hashlib.sha256(image_jpeg).digest()
        scene, objects, keywords, risk, advice = self._SCENES[digest[0] % len(self._SCENES)]
        payload = {
            "scene": scene,
            "objects": objects,
            "ocr_text": "",
            "keywords": keywords,
            "risk_level": risk,
            "advice": advice,
        }
        return VLResponse(
            raw_text=json.dumps(payload, ensure_ascii=False),
            model=self.MODEL,
            prompt_tokens=len(image_jpeg) // 1024 + 80,
            completion_tokens=40,
            latency_ms=int((time.perf_counter() - start) * 1000),
            extra={"kb_context_used": len(kb_context)},
        )

    async def ocr(self, image_jpeg: bytes) -> VLResponse:
        start = time.perf_counter()
        if self._latency_ms > 0:
            import asyncio

            await asyncio.sleep(self._latency_ms / 1000)

        digest = hashlib.sha256(image_jpeg).digest()
        text = self._DOCUMENTS[digest[1] % len(self._DOCUMENTS)]
        return VLResponse(
            raw_text=text,
            model=self.OCR_MODEL,
            prompt_tokens=len(image_jpeg) // 1024 + 40,
            completion_tokens=len(text) * 2,  # 中文约 2 token/字
            latency_ms=int((time.perf_counter() - start) * 1000),
        )

    async def close(self) -> None:
        return None


class DashScopeBackend:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float,
        max_tokens: int,
        ocr_model: str,
        ocr_timeout_s: float,
        ocr_max_tokens: int,
    ) -> None:
        import httpx

        if not api_key:
            raise ValueError("inference_backend=dashscope 但 DASHSCOPE_API_KEY 为空")
        self._model = model
        self._max_tokens = max_tokens
        # 阅读模式共用同一个连接池，只在单次请求上覆盖 model / max_tokens / timeout
        self._ocr_model = ocr_model
        self._ocr_timeout_s = ocr_timeout_s
        self._ocr_max_tokens = ocr_max_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=min(3.0, timeout_s)),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def infer(self, image_jpeg: bytes, kb_context: list[str]) -> VLResponse:
        return await self._chat(
            image_jpeg,
            model=self._model,
            max_tokens=self._max_tokens,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(kb_context),
        )

    async def ocr(self, image_jpeg: bytes) -> VLResponse:
        """阅读模式：换模型档位 + 放宽 max_tokens 和超时，其余复用同一连接池。"""
        return await self._chat(
            image_jpeg,
            model=self._ocr_model,
            max_tokens=self._ocr_max_tokens,
            system_prompt=OCR_SYSTEM_PROMPT,
            user_prompt=OCR_USER_PROMPT,
            timeout_s=self._ocr_timeout_s,
        )

    async def _chat(
        self,
        image_jpeg: bytes,
        *,
        model: str,
        max_tokens: int,
        system_prompt: str,
        user_prompt: str,
        timeout_s: float | None = None,
    ) -> VLResponse:
        import httpx

        data_uri = "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode()
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": user_prompt},
                    ],
                },
            ],
        }
        request_kwargs: dict = {"json": body}
        if timeout_s is not None:
            request_kwargs["timeout"] = httpx.Timeout(
                timeout_s, connect=min(3.0, timeout_s)
            )

        start = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", **request_kwargs)
        except httpx.TimeoutException as exc:
            raise VLTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise VLCallFailed(str(exc)) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)

        if resp.status_code != 200:
            raise VLCallFailed(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            if isinstance(text, list):  # 兼容分段 content
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            usage = data.get("usage") or {}
        except (KeyError, IndexError, ValueError) as exc:
            raise VLCallFailed(f"响应结构异常: {exc}") from exc

        return VLResponse(
            raw_text=text or "",
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        await self._client.aclose()


def build_backend(settings) -> VLBackend:
    if settings.inference_backend == "mock":
        return MockBackend(latency_ms=settings.mock_latency_ms)
    if settings.inference_backend == "dashscope":
        return DashScopeBackend(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
            model=settings.vl_model,
            timeout_s=settings.vl_timeout_s,
            max_tokens=settings.vl_max_tokens,
            ocr_model=settings.ocr_model,
            ocr_timeout_s=settings.ocr_timeout_s,
            ocr_max_tokens=settings.ocr_max_tokens,
        )
    raise ValueError(f"未知 inference_backend: {settings.inference_backend}")
