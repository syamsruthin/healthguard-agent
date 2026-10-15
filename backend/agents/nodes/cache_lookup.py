"""
Semantic answer cache nodes, scoped so answers never cross users.

* `cache_first` — before any LLM call, for a conversation's opening question
  (no history to resolve): the key is the normalised message, so a hit skips
  planning entirely. Members read their own scope, then public answers.
* `cache_lookup` — after planning, for follow-ups: the key is the standalone
  rewrite, and the scope follows whether the question needs member data.
"""

from __future__ import annotations

import time
from typing import Any

from backend.agents.guardrails import prefilter
from backend.agents.state import AgentState
from backend.agents.tools.base import UserContext
from backend.agents.trace import append, emit, step, traced
from backend.cache.semantic import CacheHit, cache_scope, get_semantic_cache, lookup_key, lookup_scopes
from backend.core import observability as obs
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import CACHE_EVENTS

log = get_logger(__name__)


async def _lookup(query: str, scopes: list[str], data_version: int) -> CacheHit | None:
    with obs.observe(
        "semantic_cache.lookup",
        as_type="retriever",
        input=query,
        metadata={
            "scopes": scopes,
            "data_version": data_version,
            "distance_threshold": settings.semantic_cache_distance_threshold,
        },
    ) as o:
        try:
            hit = await get_semantic_cache().lookup(query, scopes=scopes, data_version=data_version)
        except Exception as exc:  # the cache must never break a turn
            CACHE_EVENTS.labels(layer="semantic", outcome="error").inc()
            log.warning("semantic_cache_lookup_failed", error=str(exc)[:200])
            o.fail(exc, level="WARNING")
            return None
        CACHE_EVENTS.labels(layer="semantic", outcome="hit" if hit else "miss").inc()
        o.update(
            output={"hit": False}
            if hit is None
            else {
                "hit": True,
                "scope": hit.scope,
                "distance": round(hit.distance, 4),
                "cached_query": hit.payload.get("standalone_query"),
            }
        )
        return hit


def _served(state: AgentState, node: str, hit: CacheHit, query: str, ms: float, scope: str) -> dict[str, Any]:
    answer = hit.payload.get("answer", "")
    citations = hit.payload.get("citations", [])
    emit({"type": "token", "text": answer})
    emit({"type": "citations", "citations": citations})
    update: dict[str, Any] = {
        "cache_hit": True,
        "cache_checked": True,
        "cache_scope": scope,
        "cache_query": query,
        "outcome": "cache_hit",
        "answer": answer,
        "citations": citations,
        "trace": append(
            state,
            step(
                node,
                input=query,
                output="hit",
                duration_ms=ms,
                scope=scope,
                distance=round(hit.distance, 4),
                cached_query=hit.payload.get("standalone_query"),
            ),
        ),
    }
    if not state.get("standalone_query") and hit.payload.get("standalone_query"):
        update["standalone_query"] = hit.payload["standalone_query"]
    return update


@traced("cache_first", "Checking recent answers", as_type="retriever")
async def cache_first(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    user = UserContext.from_dict(state["user"])
    query = lookup_key(dict(state))
    with obs.observe("guardrail.prefilter", as_type="guardrail", input=state["message"]) as g:
        reason = prefilter(state["message"], user)
        g.update(output={"blocked": bool(reason), "reason": reason})
    if reason:  # `understand` blocks it without a model call
        return {"trace": append(state, step("cache_first", input=query, output="skipped: prefilter"))}
    scopes = lookup_scopes(user, needs_member_data=False)  # own scope first, then public
    hit = await _lookup(query, scopes, state.get("data_version", 0))
    ms = (time.monotonic() - t0) * 1000
    if hit is None:
        return {
            "cache_checked": True,
            "cache_query": query,
            "trace": append(
                state, step("cache_first", input=query, output="miss", duration_ms=ms, scopes=scopes)
            ),
        }
    return _served(state, "cache_first", hit, query, ms, scope=hit.scope)


@traced("cache_lookup", "Checking recent answers", as_type="retriever")
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
    hit = await _lookup(
        query, lookup_scopes(user, state.get("needs_member_data", False)), state.get("data_version", 0)
    )
    ms = (time.monotonic() - t0) * 1000
    if hit is None:
        return {
            "cache_scope": scope,
            "cache_query": query,
            "trace": append(
                state, step("cache_lookup", input=query, output="miss", duration_ms=ms, scope=scope)
            ),
        }
    return _served(state, "cache_lookup", hit, query, ms, scope=scope)
