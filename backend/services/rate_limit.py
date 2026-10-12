"""Fixed-window rate limiting in Redis (fails open if Redis is unavailable)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def seconds_to_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()), 1)


async def daily_hit(key: str, limit: int) -> None:
    """Count one request against a per-UTC-day budget (`limit` 0 disables it)."""
    if limit <= 0:
        return
    redis_key = f"hg:daily:{datetime.now(UTC):%Y%m%d}:{key}"
    try:
        redis = get_redis()
        count = await redis.incr(redis_key)
        if count == 1:
            await redis.expire(redis_key, seconds_to_utc_midnight() + 60)
    except Exception as exc:
        log.warning("rate_limit_unavailable", error=str(exc)[:200])
        return
    if count > limit:
        raise RateLimitError(
            f"Daily limit of {limit} questions reached — it resets at midnight UTC.",
            code="daily_limit",
            headers={"Retry-After": str(seconds_to_utc_midnight())},
        )
