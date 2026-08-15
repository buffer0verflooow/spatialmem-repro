"""KV 抽象：内存实现（dev/test，无外部依赖）+ Redis 实现（生产）。

令牌桶做成 KV 的原子原语，而不是在业务层读-改-写——那样多 worker 下会漏放行。
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Protocol, runtime_checkable

_BUCKET_LUA = """
local key  = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now  = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
local delta = now - ts
if delta < 0 then delta = 0 end
tokens = math.min(burst, tokens + delta * rate)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return allowed
"""


@runtime_checkable
class KV(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_s: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def take_token(
        self, key: str, rate: float, burst: float, cost: float = 1.0
    ) -> bool: ...
    async def close(self) -> None: ...


class MemoryKV:
    """进程内 KV。仅用于单 worker 开发和单测；多 worker 生产必须换 Redis。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = asyncio.Lock()

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and time.monotonic() >= expires_at

    async def get(self, key: str) -> str | None:
        async with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires_at = item
            if self._expired(expires_at):
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: str, ttl_s: float | None = None) -> None:
        async with self._lock:
            expires_at = time.monotonic() + ttl_s if ttl_s else None
            self._data[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def take_token(
        self, key: str, rate: float, burst: float, cost: float = 1.0
    ) -> bool:
        async with self._lock:
            now = time.monotonic()
            tokens, ts = self._buckets.get(key, (burst, now))
            tokens = min(burst, tokens + max(0.0, now - ts) * rate)
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            self._buckets[key] = (tokens, now)
            return allowed

    async def close(self) -> None:
        self._data.clear()
        self._buckets.clear()


class RedisKV:
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
        self._bucket = self._client.register_script(_BUCKET_LUA)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl_s: float | None = None) -> None:
        if ttl_s:
            await self._client.set(key, value, px=int(ttl_s * 1000))
        else:
            await self._client.set(key, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def take_token(
        self, key: str, rate: float, burst: float, cost: float = 1.0
    ) -> bool:
        result = await self._bucket(keys=[key], args=[rate, burst, time.time(), cost])
        return bool(int(result))

    async def close(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        return bool(await self._client.ping())


def build_kv(backend: str, redis_url: str) -> KV:
    if backend == "redis":
        return RedisKV(redis_url)
    if backend == "memory":
        return MemoryKV()
    raise ValueError(f"未知 kv_backend: {backend}")


def bucket_burst(rate: float) -> float:
    """桶容量：至少 1，允许 1 秒的突发量。"""
    return max(1.0, math.ceil(rate))
