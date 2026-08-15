"""RAG 上下文预取（CLAUDE.md §5.2）——本方案唯一真实的并行。

RAG 的 query 是模型输出的关键词，所以检索不可能与当前帧的识别并行。
但视频帧在时间上连续、场景高度相关，于是：

    用第 N 帧关键词预取的上下文，服务第 N+1 帧的推理。

检索耗时（向量化 ~30ms + 查询 ~15ms）因此完全不进入延迟预算。
会话首帧无上下文，接受首帧质量略降。
"""

from __future__ import annotations

import json

from app.kb.store import KbStore
from app.observability import get_logger
from app.observability.metrics import kb_prefetch_total
from app.storage import KV
from app.storage import keys as k

log = get_logger(__name__)


async def prefetch(
    *,
    kv: KV,
    kb: KbStore,
    device_id: str,
    keywords: list[str],
    top_k: int,
    min_score: float,
    ttl_s: int,
) -> list[str]:
    """旁路任务，绝不在主回路 await。异常只记日志，不向上抛。"""
    if not keywords:
        kb_prefetch_total.labels(outcome="empty").inc()
        return []
    try:
        hits = await kb.search(" ".join(keywords), top_k, min_score)
        if hits:
            await kv.set(k.kb_ctx(device_id), json.dumps(hits, ensure_ascii=False), ttl_s=ttl_s)
            kb_prefetch_total.labels(outcome="ok").inc()
        else:
            kb_prefetch_total.labels(outcome="empty").inc()
        return hits
    except Exception as exc:
        kb_prefetch_total.labels(outcome="error").inc()
        log.warning("kb_prefetch_failed", device_id=device_id, error=str(exc))
        return []


async def load_context(kv: KV, device_id: str) -> list[str]:
    """读取上一帧预取的上下文。读不到就返回空，不阻塞、不报错。"""
    raw = await kv.get(k.kb_ctx(device_id))
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else []
    except ValueError:
        return []
