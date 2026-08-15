"""FastAPI 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.observability import get_logger, setup_logging
from app.runtime import AppContext
from app.transport import http as http_routes
from app.transport import ws as ws_routes
from app.agent import http as agent_http
from app.observe import router as observe_routes

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
        title="spatialmem-server",
        description="统一服务端：智能眼镜全链路 + 空间记忆结构化观察",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(ws_routes.router)
    app.include_router(http_routes.router)
    app.include_router(http_routes.admin)
    app.include_router(agent_http.router)
    app.include_router(observe_routes)

    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针：不查外部依赖。"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict:
        """就绪探针：查 KV 连通性。KV 挂了就不该接流量（闸门依赖它）。"""
        ctx: AppContext = app.state.ctx
        checks = {"kv": False, "kb_ready": ctx.kb.ready}
        try:
            await ctx.kv.set("readyz", "1", ttl_s=5)
            checks["kv"] = (await ctx.kv.get("readyz")) == "1"
        except Exception as exc:
            log.warning("readyz_kv_failed", error=str(exc))

        if not checks["kv"]:
            response.status_code = 503
        return {"status": "ok" if checks["kv"] else "degraded", **checks}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
