"""Fixed-window rate limiting in Redis (fails open if Redis is unavailable)."""

from __future__ import annotations

from backend.core.errors import RateLimitError
from backend.core.logging import get_logger
from backend.core.redis import get_redis

log = get_logger(__name__)


async def hit(key: str, limit: int, window_seconds: int = 60) -> None:
    """Count one request against `key`; raise RateLimitError once `limit` is exceeded in the window."""
    redis_key = f"hg:rl:{key}"
    try:
        redis = get_redis()
        count = await redis.incr(redis_key)
        if count == 1:
            await redis.expire(redis_key, window_seconds)
        if count > limit:
            ttl = await redis.ttl(redis_key)
            raise RateLimitError(
                "Too many requests — please slow down.",
                headers={"Retry-After": str(max(int(ttl), 1))},
            )
    except RateLimitError:
        raise
    except Exception as exc:
        log.warning("rate_limit_unavailable", error=str(exc)[:200])
