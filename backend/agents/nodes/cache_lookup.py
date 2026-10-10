"""`cache_lookup` — semantic answer cache, scoped so answers never cross users."""

from __future__ import annotations

import time
from typing import Any

from backend.agents.state import AgentState
from backend.agents.tools.base import UserContext
from backend.agents.trace import append, emit, step, traced
from backend.cache.semantic import cache_scope, get_semantic_cache, lookup_key, lookup_scopes
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import CACHE_EVENTS

log = get_logger(__name__)


@traced("cache_lookup", "Checking recent answers")
async def cache_lookup(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    user = UserContext.from_dict(state["user"])
    scope = cache_scope(user, state.get("needs_member_data", False))
    query = lookup_key(dict(state))
    if not settings.semantic_cache_enabled or scope is None:
        return {
            "cache_scope": scope or "",
            "cache_query": query,
            "trace": append(state, step("cache_lookup", input=query, output="skipped", scope=scope)),
        }
    try:
        hit = await get_semantic_cache().lookup(
            query,
            scopes=lookup_scopes(user, state.get("needs_member_data", False)),
            data_version=state.get("data_version", 0),
        )
    except Exception as exc:  # the cache must never break a turn
        CACHE_EVENTS.labels(layer="semantic", outcome="error").inc()
        log.warning("semantic_cache_lookup_failed", error=str(exc)[:200])
        hit = None
    ms = (time.monotonic() - t0) * 1000
    if hit is None:
        CACHE_EVENTS.labels(layer="semantic", outcome="miss").inc()
        return {
            "cache_scope": scope,
            "cache_query": query,
            "trace": append(
                state, step("cache_lookup", input=query, output="miss", duration_ms=ms, scope=scope)
            ),
        }
    CACHE_EVENTS.labels(layer="semantic", outcome="hit").inc()
    answer = hit.payload.get("answer", "")
    citations = hit.payload.get("citations", [])
    emit({"type": "token", "text": answer})
    emit({"type": "citations", "citations": citations})
    return {
        "cache_hit": True,
        "cache_scope": scope,
        "cache_query": query,
        "outcome": "cache_hit",
        "answer": answer,
        "citations": citations,
        "trace": append(
            state,
            step(
                "cache_lookup",
                input=query,
                output="hit",
                duration_ms=ms,
                scope=scope,
                distance=round(hit.distance, 4),
                cached_query=hit.payload.get("standalone_query"),
            ),
        ),
    }
