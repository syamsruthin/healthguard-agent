from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend.core.redis import get_redis
from backend.db.session import SessionLocal

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc.__class__.__name__}"
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
    )
