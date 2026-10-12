"""Shared async Redis client (exact LLM cache, rate limiting, arq queue)."""

from __future__ import annotations

from redis.asyncio import Redis

from backend.core.config import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True, health_check_interval=30)
    return _redis


def set_redis(client: Redis | None) -> None:
    """Override the client (tests use fakeredis)."""
    global _redis
    _redis = client


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
