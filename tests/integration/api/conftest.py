from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.graph import build_graph, set_graph
from backend.cache.semantic import InMemorySemanticCache, set_semantic_cache
from backend.core.config import settings
from backend.core.redis import get_redis
from backend.main import create_app
from backend.workers.queue import RecordingEnqueuer, set_enqueuer

API = settings.api_prefix
TEST_TITLE = "pytest"  # conversations with this title are deleted after the test session


@pytest.fixture
async def jobs() -> AsyncIterator[RecordingEnqueuer]:
    recorder = RecordingEnqueuer()
    set_enqueuer(recorder)
    yield recorder
    set_enqueuer(None)


@pytest.fixture
async def client(jobs: RecordingEnqueuer) -> AsyncIterator[httpx.AsyncClient]:
    """API client without the lifespan: the singletons it would create are installed here."""
    redis = get_redis()
    for pattern in ("hg:rl:*", "hg:turn:*"):
        async for key in redis.scan_iter(match=pattern):
            await redis.delete(key)
    set_graph(build_graph(InMemorySaver()))
    set_semantic_cache(InMemorySemanticCache())
    transport = httpx.ASGITransport(app=create_app(with_lifespan=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    set_graph(None)
    set_semantic_cache(None)


async def login(client: httpx.AsyncClient, email: str, password: str | None = None) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/auth/login", json={"email": email, "password": password or settings.demo_password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def auth(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def register(client: httpx.AsyncClient) -> dict[str, Any]:
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": email, "password": "correct-horse-battery", "full_name": "Test User"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def parse_sse(body: str) -> list[dict[str, Any]]:
    events, name, data = [], None, []
    for line in body.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line.strip() and name:
            events.append({"type": name, **json.loads("\n".join(data) or "{}")})
            name, data = None, []
    if name:
        events.append({"type": name, **json.loads("\n".join(data) or "{}")})
    return events
