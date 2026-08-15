"""推理节点——管线里唯一含模型调用的节点。

延迟预算里模型调用占 P50 的 94%（CLAUDE.md §6），所以这个节点的纪律是：
  - 单请求 1 次调用（例外见 §5.3）
  - 重试 1 次而非 3 次，3 次会击穿 P95
  - RAG 检索、日志落库全部丢到旁路，不 await
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from app.config import Settings
from app.inference.backend import VLBackend, VLResponse, VLTimeout
from app.inference.image import normalize
from app.inference.parser import parse, parse_ocr
from app.inference.schema import VLResult
from app.kb import KbStore, load_context, prefetch
from app.observability import get_logger
from app.observability.metrics import (
    model_calls_total,
    model_tokens_total,
    second_call_total,
    timed,
)
from app.storage import KV

log = get_logger(__name__)

NODE = "infer"

Spawn = Callable[[Coroutine[Any, Any, Any], str], None]


def make_infer_node(
    *,
    backend: VLBackend,
    kb: KbStore,
    kv: KV,
    settings: Settings,
    spawn: Spawn,
):
    async def infer_node(state: dict[str, Any]) -> dict[str, Any]:
        with timed(NODE):
            return await _run(state, backend, kb, kv, settings, spawn)

    return infer_node


async def _run(
    state: dict[str, Any],
    backend: VLBackend,
    kb: KbStore,
    kv: KV,
    settings: Settings,
    spawn: Spawn,
) -> dict[str, Any]:
    if state.get("trigger") == "read":
        return await _run_read(state, backend, settings)

    device_id: str = state["device_id"]

    image, recoded = normalize(
        state["frame_jpeg"],
        max_edge=settings.image_max_edge,
        quality=settings.image_jpeg_quality,
    )

    kb_context = await load_context(kv, device_id)

    resp, error = await _call(backend, image, kb_context, settings)
    if error is not None:
        return {
            "error": error,
            "kb_context": kb_context,
            "vl_meta": {"model": settings.vl_model, "recoded": recoded},
        }

    result, degraded = parse(resp.raw_text)
    meta: dict[str, Any] = {
        "model": resp.model,
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
        "latency_ms": resp.latency_ms,
        "recoded": recoded,
        "degraded_parse": degraded,
        "second_call": False,
        "image_bytes": len(image),
    }
    _count_tokens(resp)

    # §5.3 唯一例外：高风险且首帧无上下文时，补一次带 RAG 的复核
    if (
        settings.second_call_enabled
        and result.risk_level == "high"
        and not kb_context
        and result.keywords
    ):
        result, meta = await _recheck(
            result, meta, backend, kb, image, settings
        )

    spawn(
        prefetch(
            kv=kv,
            kb=kb,
            device_id=device_id,
            keywords=result.keywords,
            top_k=settings.kb_top_k,
            min_score=settings.kb_min_score,
            ttl_s=settings.kb_ctx_ttl_s,
        ),
        "kb_prefetch",
    )

    return {
        "vl_result": result.model_dump(),
        "vl_meta": meta,
        "kb_context": kb_context,
        "error": None,
    }


async def _run_read(
    state: dict[str, Any],
    backend: VLBackend,
    settings: Settings,
) -> dict[str, Any]:
    """阅读模式：换 OCR 档位，跳过 RAG 与复核。

    这条路径**故意**不做三件事：
      - 不读预取上下文：领域知识对逐字转录没有价值
      - 不触发 kb_prefetch：keywords 为空，检索是白花的
      - 不走 §5.3 的二次复核：那是给 risk_level=high 的，阅读模式没这概念
    """
    image, recoded = normalize(
        state["frame_jpeg"],
        max_edge=settings.image_max_edge,
        quality=settings.image_jpeg_quality,
    )

    resp, error = await _call_ocr(backend, image, settings)
    if error is not None:
        return {
            "error": error,
            "kb_context": [],
            "vl_meta": {"model": settings.ocr_model, "recoded": recoded, "mode": "read"},
        }

    result = parse_ocr(resp.raw_text, settings.ocr_max_chars)
    _count_tokens(resp)

    return {
        "vl_result": result.model_dump(),
        "vl_meta": {
            "model": resp.model,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "latency_ms": resp.latency_ms,
            "recoded": recoded,
            "degraded_parse": False,
            "second_call": False,
            "image_bytes": len(image),
            "mode": "read",
        },
        "kb_context": [],
        "error": None,
    }


async def _recheck(
    result: VLResult,
    meta: dict[str, Any],
    backend: VLBackend,
    kb: KbStore,
    image: bytes,
    settings: Settings,
) -> tuple[VLResult, dict[str, Any]]:
    extra = await kb.search(
        " ".join(result.keywords), settings.kb_top_k, settings.kb_min_score
    )
    if not extra:
        return result, meta

    second_call_total.inc()
    resp, error = await _call(backend, image, extra, settings)
    if error is not None:
        log.warning("second_call_failed_keep_first_result", error=error)
        return result, meta

    rechecked, degraded = parse(resp.raw_text)
    _count_tokens(resp)
    meta = {
        **meta,
        "second_call": True,
        "prompt_tokens": meta["prompt_tokens"] + resp.prompt_tokens,
        "completion_tokens": meta["completion_tokens"] + resp.completion_tokens,
        "latency_ms": meta["latency_ms"] + resp.latency_ms,
        "degraded_parse": meta["degraded_parse"] or degraded,
    }
    return rechecked, meta


async def _call(
    backend: VLBackend,
    image: bytes,
    kb_context: list[str],
    settings: Settings,
) -> tuple[VLResponse | None, str | None]:
    """实时帧调用。attempts = vl_retries + 1。"""
    return await _call_with_policy(
        lambda: backend.infer(image, kb_context),
        timeout_s=settings.vl_timeout_s,
        retries=settings.vl_retries,
        model_label=settings.vl_model,
    )


async def _call_ocr(
    backend: VLBackend, image: bytes, settings: Settings
) -> tuple[VLResponse | None, str | None]:
    """阅读模式调用。超时预算独立且宽得多——用户主动发起，愿意等。"""
    return await _call_with_policy(
        lambda: backend.ocr(image),
        timeout_s=settings.ocr_timeout_s,
        retries=settings.ocr_retries,
        model_label=settings.ocr_model,
    )


async def _call_with_policy(
    call: Callable[[], Any],
    *,
    timeout_s: float,
    retries: int,
    model_label: str,
) -> tuple[VLResponse | None, str | None]:
    """带超时和有限重试的调用。attempts = retries + 1。永不抛异常。"""
    attempts = max(1, retries + 1)
    last_error = "unknown"

    for attempt in range(attempts):
        try:
            async with asyncio.timeout(timeout_s):
                resp = await call()
            model_calls_total.labels(model=resp.model, outcome="ok").inc()
            return resp, None
        except (TimeoutError, VLTimeout) as exc:
            last_error = f"timeout: {exc}"
            model_calls_total.labels(model=model_label, outcome="timeout").inc()
        except Exception as exc:  # 兜住一切（含 VLCallFailed），绝不让主流程崩
            last_error = f"call_failed: {exc}"
            model_calls_total.labels(model=model_label, outcome="error").inc()

        if attempt + 1 < attempts:
            log.info("vl_retry", attempt=attempt + 1, error=last_error)

    log.warning("vl_call_exhausted", attempts=attempts, error=last_error)
    return None, last_error


def _count_tokens(resp: VLResponse) -> None:
    if resp.prompt_tokens:
        model_tokens_total.labels(model=resp.model, kind="prompt").inc(resp.prompt_tokens)
    if resp.completion_tokens:
        model_tokens_total.labels(model=resp.model, kind="completion").inc(
            resp.completion_tokens
        )
