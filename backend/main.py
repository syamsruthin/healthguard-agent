"""
HealthGuard API — application factory.

    uvicorn backend.main:app

Lifespan: Postgres checkpointer + agent graph, arq job queue, semantic-cache
index, sandbox warm-up; everything is closed in reverse order on shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from backend.agents.graph import build_graph, open_checkpointer, set_graph
from backend.agents.tools import sandbox
from backend.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from backend.api.routers import admin, auth, conversations, feedback, health, me
from backend.cache.semantic import get_semantic_cache
from backend.core.config import settings
from backend.core.errors import install_error_handlers
from backend.core.logging import configure_logging, get_logger
from backend.core.redis import close_redis
from backend.core.telemetry import setup_tracing
from backend.db.session import dispose
from backend.services.chat_service import drain_background
from backend.workers.queue import ArqEnqueuer, set_enqueuer

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool, saver = await open_checkpointer()
    set_graph(build_graph(saver))
    enqueuer = await ArqEnqueuer.create()
    set_enqueuer(enqueuer)
    try:
        await get_semantic_cache().ensure_index()
    except Exception as exc:  # the cache is optional
        log.warning("semantic_cache_unavailable", error=str(exc)[:200])
    warm = asyncio.create_task(sandbox.warm_up())
    log.info("api_started", env=settings.env)
    try:
        yield
    finally:
        await drain_background()
        warm.cancel()
        sandbox.shutdown()
        set_enqueuer(None)
        await enqueuer.close()
        set_graph(None)
        await pool.close()
        await close_redis()
        await dispose()
        log.info("api_stopped")


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    configure_logging()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan if with_lifespan else None,
        docs_url="/docs" if settings.env != "production" else None,
        redoc_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=64 * 1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # bearer tokens, no cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    for router in (auth.router, conversations.router, feedback.router, me.router, admin.router):
        app.include_router(router, prefix=settings.api_prefix)

    Instrumentator(excluded_handlers=["/metrics", "/health/.*"]).instrument(app).expose(
        app, include_in_schema=False
    )
    setup_tracing(app)
    return app


app = create_app()
