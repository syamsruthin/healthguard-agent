from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import fakeredis
import pytest

from backend.cache.semantic import InMemorySemanticCache, set_semantic_cache
from backend.core.redis import set_redis


@pytest.fixture(autouse=True)
async def fake_redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    """Unit tests never touch a real Redis."""
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    set_redis(client)
    yield client
    set_redis(None)
    await client.aclose()


@pytest.fixture(autouse=True)
def memory_semantic_cache() -> Iterator[InMemorySemanticCache]:
    """Graph tests use an in-process semantic cache (fakeredis has no vector search)."""
    cache = InMemorySemanticCache()
    set_semantic_cache(cache)
    yield cache
    set_semantic_cache(None)
