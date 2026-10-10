from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis
import pytest

from backend.core.redis import set_redis


@pytest.fixture(autouse=True)
async def fake_redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    """Unit tests never touch a real Redis."""
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    set_redis(client)
    yield client
    set_redis(None)
    await client.aclose()
