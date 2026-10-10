"""
Exact-match LLM response cache (Redis).

Only deterministic (temperature 0, non-streaming) calls are cached. The key
hashes the model, fallbacks, full message list, parameters and prompt
version, so a cached completion is only ever returned for byte-identical
input — it never crosses a context boundary it could not already see.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.core.telemetry import CACHE_EVENTS

log = get_logger(__name__)
_PREFIX = "hg:llm:"


def cache_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return _PREFIX + hashlib.sha256(blob).hexdigest()


async def get(key: str) -> dict[str, Any] | None:
    if not settings.llm_exact_cache_enabled:
        return None
    try:
        raw = await get_redis().get(key)
    except Exception as exc:  # cache must never break the request path
        log.warning("llm_cache_get_failed", error=str(exc))
        return None
    CACHE_EVENTS.labels(layer="llm_exact", outcome="hit" if raw else "miss").inc()
    return json.loads(raw) if raw else None


async def put(key: str, value: dict[str, Any]) -> None:
    if not settings.llm_exact_cache_enabled:
        return
    try:
        await get_redis().set(key, json.dumps(value), ex=settings.llm_exact_cache_ttl_seconds)
    except Exception as exc:
        log.warning("llm_cache_put_failed", error=str(exc))
