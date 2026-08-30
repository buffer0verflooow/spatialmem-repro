"""观察后端：mock（确定性假响应，无 key 可跑）与 dashscope（真实 VLM）。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Protocol, runtime_checkable

import httpx

from .prompts import OBSERVE_SYSTEM_PROMPT, build_observe_user_prompt


@runtime_checkable
class ObserveBackend(Protocol):
    async def observe(self, jpeg: bytes, hint: str) -> dict: ...
    async def close(self) -> None: ...


class MockObserveBackend:
    """确定性假响应：同一张图永远得到同一结果，便于测试与无 key 联调。"""

    _SCENES = [
        {"name": "电动剃须刀", "color": "蓝色", "location": "在地上",
         "attributes": "飞利浦,电动", "confidence": 0.9,
         "support": {"name": "地面", "color": "", "location": "浴室",
                     "attributes": "瓷砖"},
         "anchors": [{"type": "door", "name": "门", "direction": "left",
                      "distance_m": 2.5, "confidence": 0.9}]},
        {"name": "小磨香油", "color": "深色", "location": "在桌上",
         "attributes": "瓶装,调味品", "confidence": 0.9,
         "support": {"name": "桌子", "color": "棕色", "location": "餐厅",
                     "attributes": "木质"},
         "anchors": [{"type": "window", "name": "窗户", "direction": "front",
                      "distance_m": 3.0, "confidence": 0.85}]},
        {"name": "电风扇", "color": "白色", "location": "在地上",
         "attributes": "落地扇", "confidence": 0.85,
         "support": {"name": "地面", "color": "", "location": "客厅",
                     "attributes": "木地板"},
         "anchors": [{"type": "wall", "name": "墙", "direction": "back",
                      "distance_m": 1.5, "confidence": 0.95}]},
        {"name": "鼠标", "color": "黑色", "location": "在桌上",
         "attributes": "有线", "confidence": 0.85,
         "support": {"name": "桌子", "color": "原木色", "location": "书房",
                     "attributes": "木质,长条桌"},
         "anchors": [{"type": "door", "name": "门", "direction": "right",
                      "distance_m": 2.0, "confidence": 0.88}]},
        {"name": "", "color": "", "location": "",
         "attributes": "", "confidence": 0.0,
         "support": {"name": "", "color": "", "location": "", "attributes": ""},
         "anchors": []},
    ]

    def __init__(self, latency_ms: int = 0) -> None:
        self._latency_ms = latency_ms

    async def observe(self, jpeg: bytes, hint: str) -> dict:
        if self._latency_ms > 0:
            import asyncio

            await asyncio.sleep(self._latency_ms / 1000)
        scene = self._SCENES[hashlib.sha256(jpeg).digest()[0] % len(self._SCENES)]
        return dict(scene)

    async def close(self) -> None:
        return None


class DashScopeObserveBackend:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float,
        max_tokens: int,
    ) -> None:
        if not api_key:
            raise ValueError("inference_backend=dashscope 但 DASHSCOPE_API_KEY 为空")
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=min(3.0, timeout_s)),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def observe(self, jpeg: bytes, hint: str) -> dict:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": OBSERVE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": build_observe_user_prompt(hint)},
                    ],
                },
            ],
        }
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise ValueError(f"observe timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"observe call failed: {exc}") from exc
        if resp.status_code != 200:
            raise ValueError(f"observe HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            text = resp.json()["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"observe 响应结构异常: {exc}") from exc
        parsed = _parse_json(text)
        if not parsed:
            raise ValueError("observe 输出不是合法 JSON")
        support_raw = parsed.get("support")
        support = (
            {
                "name": str(support_raw.get("name", "")).strip(),
                "color": str(support_raw.get("color", "")).strip(),
                "location": str(support_raw.get("location", "")).strip(),
                "attributes": str(support_raw.get("attributes", "")).strip(),
            }
            if isinstance(support_raw, dict)
            else {"name": "", "color": "", "location": "", "attributes": ""}
        )
        return {
            "name": str(parsed.get("name", "")).strip(),
            "color": str(parsed.get("color", "")).strip(),
            "location": str(parsed.get("location", "")).strip(),
            "attributes": str(parsed.get("attributes", "")).strip(),
            "confidence": float(parsed.get("confidence", 0.0)),
            "support": support,
            "anchors": _normalize_anchors(parsed.get("anchors")),
        }

    async def close(self) -> None:
        await self._client.aclose()


def build_observe_backend(settings) -> ObserveBackend:
    if settings.inference_backend == "mock":
        return MockObserveBackend(latency_ms=settings.mock_latency_ms)
    if settings.inference_backend == "dashscope":
        return DashScopeObserveBackend(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
            model=settings.observe_model,
            timeout_s=settings.observe_timeout_s,
            max_tokens=settings.observe_max_tokens,
        )
    raise ValueError(f"未知 inference_backend: {settings.inference_backend}")


def _parse_json(text: str) -> dict | None:
    import re

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_anchors(raw: object) -> list[dict]:
    """把 VLM 输出的 anchors 归一化为稳定结构，过滤非法类型/空名。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        atype = str(item.get("type", "")).strip().lower()
        aname = str(item.get("name", "")).strip()
        if atype not in ("door", "window", "wall") or not aname:
            continue
        direction = str(item.get("direction", "")).strip()
        if direction not in ("left", "right", "front", "back"):
            direction = ""
        out.append(
            {
                "type": atype,
                "name": aname,
                "direction": direction,
                "distance_m": float(item.get("distance_m", 0.0) or 0.0),
                "confidence": float(item.get("confidence", 0.0)),
            }
        )
    return out
