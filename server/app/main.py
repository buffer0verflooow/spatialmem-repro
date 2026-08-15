"""FastAPI 入口（独立精简版）：只提供 /v1/observe 与存活探针。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.observability import get_logger, setup_logging
from app.observe import router as observe_routes
from app.runtime import AppContext

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, pretty=(settings.env == "dev"))
    ctx = AppContext(settings)
    app.state.ctx = ctx
    await ctx.startup()
    try:
        yield
    finally:
        await ctx.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="spatialmem-observe",
        description="结构化观察服务：帧 -> VLM -> 空间记忆 JSON",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(observe_routes)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
