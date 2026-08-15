"""背压：每设备只保留最新 1 帧待处理（CLAUDE.md §5.1）。

实时场景下旧帧没有价值——绝不排队积压。队列满时丢弃旧帧、装入新帧。
"""

from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

from app.observability.metrics import backpressure_dropped_total

T = TypeVar("T")


class LatestOnlySlot(Generic[T]):
    """容量 1 的槽位：put 永不阻塞，新值覆盖旧值。"""

    def __init__(self) -> None:
        self._item: T | None = None
        self._has_item = asyncio.Event()
        self._closed = False

    def put(self, item: T) -> bool:
        """返回 True 表示挤掉了一个未处理的旧帧。"""
        dropped = self._item is not None
        if dropped:
            backpressure_dropped_total.inc()
        self._item = item
        self._has_item.set()
        return dropped

    async def get(self) -> T | None:
        """取出最新值。close() 后返回 None，用于让消费循环退出。"""
        await self._has_item.wait()
        if self._closed and self._item is None:
            return None
        item = self._item
        self._item = None
        self._has_item.clear()
        return item

    def close(self) -> None:
        self._closed = True
        self._has_item.set()

    @property
    def closed(self) -> bool:
        return self._closed
