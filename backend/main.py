"""
HealthGuard API — application factory.

`uvicorn backend.main:app` serves the API. The full router set, lifespan
resources (checkpointer, arq pool, semantic cache) and middleware are added
in the API phase; this baseline exposes the liveness probe used by the
Docker HEALTHCHECK.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from backend.core.config import settings
from backend.core.errors import install_error_handlers
from backend.core.logging import configure_logging

health = APIRouter(tags=["health"])


@health.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title=settings.app_name, version=settings.app_version)
    install_error_handlers(app)
    app.include_router(health)
    return app


app = create_app()
