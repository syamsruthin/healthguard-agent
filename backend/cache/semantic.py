"""
Semantic answer cache (Redis 8 vector search via RedisVL).

A verified answer is stored under the embedding of the turn's standalone
query and served again when a later query is close enough. Answers are
partitioned by **scope** so nothing leaks between users:

* `role:admin`           — admin analytics
* `member:{member_id}`   — anything built from (or mentioning) one member's data
* `public`               — general plan / policy / provider questions
* no cache               — member-data questions from accounts with no member profile

Entries are also tagged with the data version and prompt version, so a
re-seed or prompt change invalidates them. RedisVL's `SemanticCache` needs a
synchronous vectorizer, so this module drives an `AsyncSearchIndex` directly.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.asyncio import Redis
from redisvl.index import AsyncSearchIndex
from redisvl.query import VectorQuery
from redisvl.query.filter import Tag
from redisvl.redis.utils import array_to_buffer

from backend.agents.tools.base import UserContext
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import CACHE_EVENTS
from backend.llm.embeddings import cosine, embed_query, get_embedder, normalize_query
from backend.llm.prompts import PROMPT_VERSION

log = get_logger(__name__)


@dataclass
class CacheHit:
    payload: dict[str, Any]
    distance: float
    scope: str = ""


class SemanticCache(Protocol):
    async def lookup(self, query: str, *, scopes: list[str], data_version: int) -> CacheHit | None: ...

    async def store(
        self, query: str, payload: dict[str, Any], *, scope: str, data_version: int, ttl: int
    ) -> None: ...

    async def ensure_index(self) -> None: ...


# ── Scope rules ─────────────────────────────────────────────────────────
def cache_scope(user: UserContext, needs_member_data: bool) -> str | None:
    """Scope a lookup may read from; None disables the cache for this turn."""
    if user.is_admin:
        return "role:admin"
    if needs_member_data:
        return f"member:{user.member_id}" if user.member_id else None
    return "public"


def lookup_scopes(user: UserContext, needs_member_data: bool) -> list[str]:
    """Scopes a lookup may read: a member's own personalised answers come before public ones."""
    scope = cache_scope(user, needs_member_data)
    if scope is None:
        return []
    if scope == "public" and user.member_id and not user.is_admin:
        return [f"member:{user.member_id}", "public"]
    return [scope]


def _identity_terms(user: UserContext) -> list[str]:
    terms = [user.full_name, user.email, user.member_id, user.plan_id, user.plan_name]
    terms += [t for name in [user.full_name] if name for t in name.split()]
    for dep in user.dependents:
        terms += [dep.get("member_id"), dep.get("first_name")]
    return [t for t in terms if t and len(t) >= 3]


def store_scope(scope: str, user: UserContext, *texts: str) -> str | None:
    """Scope to store an answer under.

    A 'public' turn whose answer or rewritten question mentions who the user is
    (name, member ID, dependents, their plan) was personalised, so it becomes private.
    """
    if scope != "public":
        return scope
    lowered = " ".join(texts).lower()
    if any(re.search(rf"\b{re.escape(t.lower())}\b", lowered) for t in _identity_terms(user)):
        return f"member:{user.member_id}" if user.member_id else None
    return scope


def lookup_key(state: dict[str, Any]) -> str:
    """Text the cache is keyed on.

    A first-turn question is self-contained, so its normalised wording is a stable
    key; the LLM rewrite varies from run to run and is only needed when the
    question depends on earlier turns.
    """
    if not state.get("recent_turns") and not state.get("summary"):
        return normalize_query(state.get("message", ""))
    return state.get("standalone_query") or state.get("message", "")


def ttl_for(scope: str) -> int:
    if scope.startswith("member:"):
        return settings.semantic_cache_ttl_member_seconds
    return settings.semantic_cache_ttl_public_seconds


def cacheable(state: dict[str, Any]) -> bool:
    """Only verified, fully successful answers are worth replaying."""
    verification = state.get("verification") or {}
    return (
        state.get("outcome") == "answered"
        and not state.get("cache_hit")
        and bool(state.get("cache_scope"))
        and not verification.get("skipped")
        and bool(verification.get("grounded"))
        and bool(verification.get("complete"))
        and bool(state.get("results"))
        and all(r.get("ok") for r in state.get("results", []))
    )


def build_payload(state: dict[str, Any], *, source_message_id: str | None = None) -> dict[str, Any]:
    return {
        "answer": state.get("answer", ""),
        "citations": state.get("citations", []),
        "tools": [
            {"step_id": r["step_id"], "tool": r["tool"], "task": r["task"]} for r in state.get("results", [])
        ],
        "standalone_query": state.get("standalone_query", ""),
        "source_message_id": source_message_id,
        "created_at": datetime.now(UTC).isoformat(),
    }


async def maybe_store(
    state: dict[str, Any], user: UserContext, *, source_message_id: str | None = None
) -> str | None:
    """Store a finished turn if eligible; returns the scope used (None if not stored). Never raises."""
    if not settings.semantic_cache_enabled or not cacheable(state):
        return None
    scope = store_scope(
        state["cache_scope"], user, state.get("answer", ""), state.get("standalone_query", "")
    )
    if scope is None:
        return None
    try:
        await get_semantic_cache().store(
            state.get("cache_query") or lookup_key(state),
            build_payload(state, source_message_id=source_message_id),
            scope=scope,
            data_version=int(state.get("data_version", 0)),
            ttl=ttl_for(scope),
        )
    except Exception as exc:
        CACHE_EVENTS.labels(layer="semantic", outcome="store_error").inc()
        log.warning("semantic_cache_store_failed", error=str(exc)[:200])
        return None
    return scope


# ── Redis implementation ────────────────────────────────────────────────
def _index_name() -> str:
    emb = get_embedder()
    slug = re.sub(r"[^a-z0-9]+", "_", emb.model.lower()).strip("_")
    return f"hg_semcache_{slug}_{emb.dim}"


class RedisSemanticCache:
    def __init__(self, redis_url: str | None = None):
        # Vector bytes must not be decoded, so this client is separate from the shared one.
        self._client = Redis.from_url(redis_url or settings.redis_url, decode_responses=False)
        name = _index_name()
        self._index = AsyncSearchIndex.from_dict(
            {
                "index": {"name": name, "prefix": name, "storage_type": "hash"},
                "fields": [
                    {"name": "scope", "type": "tag"},
                    {"name": "data_version", "type": "tag"},
                    {"name": "prompt_version", "type": "tag"},
                    {
                        "name": "embedding",
                        "type": "vector",
                        "attrs": {
                            "dims": get_embedder().dim,
                            "distance_metric": "cosine",
                            "algorithm": "flat",
                            "datatype": "float32",
                        },
                    },
                ],
            },
            redis_client=self._client,
        )
        self._ready = False

    async def ensure_index(self) -> None:
        if not self._ready:
            if not await self._index.exists():
                await self._index.create(overwrite=False)
            self._ready = True

    async def lookup(self, query: str, *, scopes: list[str], data_version: int) -> CacheHit | None:
        await self.ensure_index()
        vector = await embed_query(query)
        flt = (
            (Tag("scope") == scopes)
            & (Tag("data_version") == str(data_version))
            & (Tag("prompt_version") == PROMPT_VERSION)
        )
        q = VectorQuery(
            vector=vector,
            vector_field_name="embedding",
            return_fields=["body", "scope"],
            filter_expression=flt,
            num_results=1,
            return_score=True,
        )
        rows = await self._index.query(q)
        if not rows:
            return None
        distance = float(rows[0]["vector_distance"])
        if distance > settings.semantic_cache_distance_threshold:
            return None
        payload, scope = rows[0]["body"], rows[0].get("scope", "")
        if isinstance(payload, bytes):
            payload = payload.decode()
        if isinstance(scope, bytes):
            scope = scope.decode()
        return CacheHit(payload=json.loads(payload), distance=distance, scope=scope)

    async def store(
        self, query: str, payload: dict[str, Any], *, scope: str, data_version: int, ttl: int
    ) -> None:
        await self.ensure_index()
        vector = await embed_query(query)
        await self._index.load(
            [
                {
                    "query": query,
                    "body": json.dumps(payload),
                    "scope": scope,
                    "data_version": str(data_version),
                    "prompt_version": PROMPT_VERSION,
                    "embedding": array_to_buffer(vector, "float32"),
                }
            ],
            ttl=ttl,
        )

    async def clear(self) -> None:
        await self.ensure_index()
        await self._index.clear()

    async def close(self) -> None:
        await self._client.aclose()


# ── In-memory implementation (tests, no Redis Query Engine needed) ─────
class InMemorySemanticCache:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def ensure_index(self) -> None:
        return None

    async def lookup(self, query: str, *, scopes: list[str], data_version: int) -> CacheHit | None:
        vector = await embed_query(query)
        now = time.monotonic()
        best: CacheHit | None = None
        for e in self.entries:
            if (
                e["expires"] < now
                or e["scope"] not in scopes
                or (e["data_version"], e["prompt_version"]) != (data_version, PROMPT_VERSION)
            ):
                continue
            distance = 1.0 - cosine(vector, e["embedding"])
            if distance <= settings.semantic_cache_distance_threshold and (
                best is None or distance < best.distance
            ):
                best = CacheHit(payload=e["payload"], distance=distance, scope=e["scope"])
        return best

    async def store(
        self, query: str, payload: dict[str, Any], *, scope: str, data_version: int, ttl: int
    ) -> None:
        self.entries.append(
            {
                "query": query,
                "payload": payload,
                "scope": scope,
                "data_version": data_version,
                "prompt_version": PROMPT_VERSION,
                "embedding": await embed_query(query),
                "expires": time.monotonic() + ttl,
            }
        )


_cache: SemanticCache | None = None


def get_semantic_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = RedisSemanticCache()
    return _cache


def set_semantic_cache(cache: SemanticCache | None) -> None:
    global _cache
    _cache = cache
