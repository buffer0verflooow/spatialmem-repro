"""帧准入闸门（CLAUDE.md §5.1）——本项目最核心的机制。

2 帧/秒全量处理 vs 场景变化触发，模型调用量相差 20 倍
（172,800 vs 8,640 次/设备/天）。全量处理在成本上不可行，
所以这个闸门不是附属功能，它决定项目能不能上线。

规则按顺序执行，任一驳回即丢弃：
  1. 限流       单设备模型调用 <= rate 次/秒（令牌桶，原子）
  2. 去重       与上一帧 dhash 距离 < dup_distance 判为同场景
  3. 场景门控   距上次调用不足 min_interval 且距离未超 force_distance
  4. 豁免       trigger=manual / read 跳过 2、3，仅受 1 约束

阅读模式（trigger=read）走**独立**的令牌桶：单次阅读的 completion tokens
是普通帧的 20-30 倍，必须单独限速；同时它不该挤占实时帧的额度，反之亦然。

驳回时不回传新结果（眼镜端继续显示上一结果），语义见 docs/api.md。
"""

from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.gate.phash import dhash, hamming
from app.observability import get_logger
from app.observability.metrics import reject_total, timed
from app.storage import KV, bucket_burst
from app.storage import keys as k

log = get_logger(__name__)

NODE = "gate"

REASON_RATE_LIMIT = "rate_limit"
REASON_READ_RATE_LIMIT = "read_rate_limit"
REASON_DUPLICATE = "duplicate"
REASON_NO_SCENE_CHANGE = "no_scene_change"
REASON_DECODE_FAILED = "decode_failed"


def make_gate_node(kv: KV, settings: Settings):
    async def gate_node(state: dict[str, Any]) -> dict[str, Any]:
        with timed(NODE):
            return await _run(state, kv, settings)

    return gate_node


async def _run(state: dict[str, Any], kv: KV, settings: Settings) -> dict[str, Any]:
    device_id: str = state["device_id"]
    trigger: str = state.get("trigger", "auto")
    now: float = state["timestamp"]

    # --- 1. 限流（对所有 trigger 生效，但阅读模式走独立的桶）---
    if trigger == "read":
        if not await kv.take_token(
            k.dev_read_ratelimit(device_id),
            rate=settings.gate_read_rate_per_min / 60.0,
            burst=max(1.0, settings.gate_read_burst),
        ):
            return _reject(REASON_READ_RATE_LIMIT)
    else:
        rate = settings.gate_rate_limit_per_sec
        allowed = await kv.take_token(
            k.dev_ratelimit(device_id), rate=rate, burst=bucket_burst(rate)
        )
        if not allowed:
            return _reject(REASON_RATE_LIMIT)

    # --- 计算指纹 ---
    try:
        current = dhash(state["frame_jpeg"])
    except ValueError as exc:
        return _reject(REASON_DECODE_FAILED, error=str(exc))

    last_raw = await kv.get(k.dev_lastframe(device_id))
    last = _parse_last(last_raw)
    distance = hamming(current, last["phash"]) if last else 64
    since = (now - last["ts"]) if last else None

    # --- 4. 手动触发 / 阅读模式豁免规则 2、3 ---
    # read 不豁免的话功能等于废掉：对着同一份菜单拍第二次，dhash 距离接近 0，
    # 必然被去重驳回。
    if trigger in ("manual", "read"):
        await _remember(kv, device_id, current, now, settings)
        return _pass(current, distance, since)

    if last is not None:
        # --- 2. 去重 ---
        if distance < settings.gate_phash_dup_distance:
            return _reject(REASON_DUPLICATE, phash=current, distance=distance, since=since)

        # --- 3. 场景变化门控 ---
        if (
            since is not None
            and since < settings.gate_min_interval_s
            and distance < settings.gate_force_distance
        ):
            return _reject(
                REASON_NO_SCENE_CHANGE, phash=current, distance=distance, since=since
            )

    await _remember(kv, device_id, current, now, settings)
    return _pass(current, distance, since)


def _pass(phash: str, distance: int, since: float | None) -> dict[str, Any]:
    return {
        "phash": phash,
        "hash_distance": distance,
        "since_last_call_s": since,
        "rejected_by": None,
        "reject_reason": None,
    }


def _reject(
    reason: str,
    *,
    phash: str = "",
    distance: int | None = None,
    since: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    reject_total.labels(node=NODE, reason=reason).inc()
    out: dict[str, Any] = {
        "rejected_by": NODE,
        "reject_reason": reason,
        "phash": phash,
        "hash_distance": distance,
        "since_last_call_s": since,
    }
    if error:
        out["error"] = error
    return out


async def _remember(
    kv: KV, device_id: str, phash: str, ts: float, settings: Settings
) -> None:
    """记录本次准入的帧指纹，作为下一帧的比较基准。

    TTL 取 min_interval 的 10 倍：设备停用一段时间后重新推图，
    应当视为新场景直接放行，而不是和很久以前的帧比较。
    """
    await kv.set(
        k.dev_lastframe(device_id),
        json.dumps({"phash": phash, "ts": ts}),
        ttl_s=max(60.0, settings.gate_min_interval_s * 10),
    )


def _parse_last(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {"phash": str(data["phash"]), "ts": float(data["ts"])}
    except (ValueError, KeyError, TypeError):
        return None
