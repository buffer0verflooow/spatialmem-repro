"""运行时容器（独立精简版）：仅装配结构化观察后端。"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.observability import get_logger
from app.observe import build_observe_backend
from app.observe.backend import ObserveBackend

log = get_logger(__name__)


@dataclass
class AppContext:
    """进程级单例：lifespan 中 build/close。"""

    settings: Settings
    observe: ObserveBackend

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.observe = build_observe_backend(self.settings)

    async def startup(self) -> None:
        log.info(
            "runtime_ready",
            extra={
                "env": self.settings.env,
                "inference": self.settings.inference_backend,
            },
        )

    async def shutdown(self) -> None:
        await self.observe.close()
        log.info("runtime_closed")
