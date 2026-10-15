"""One real turn exported to Langfuse and read back through the Observations API (needs LANGFUSE_* keys)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core import observability as obs
from backend.core.config import settings
from tests.integration.api.conftest import API, auth, login

pytestmark = pytest.mark.skipif(
    not settings.langfuse_configured, reason="set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY to run"
)


async def test_turn_reaches_langfuse(client) -> None:
    lf = obs.get_langfuse()
    assert lf is not None and await asyncio.to_thread(lf.auth_check), "Langfuse keys rejected"
    tokens = await login(client, "priya.patel@demo.healthguard.dev")
    cid = (await client.post(f"{API}/conversations", json={}, headers=auth(tokens))).json()["id"]
    final = (
        await client.post(
            f"{API}/conversations/{cid}/messages",
            json={"message": "What is my current HSA balance?"},
            headers=auth(tokens),
            timeout=180,
        )
    ).json()
    assert final["outcome"] in ("answered", "cache_hit"), final
    trace_id = uuid.UUID(final["trace_id"]).hex
    obs.flush()

    observations: list = []
    for _ in range(30):  # OTel ingestion is near real-time; allow for queueing
        page = await asyncio.to_thread(
            lf.api.observations.get_many, trace_id=trace_id, fields="core,basic,usage", limit=100
        )
        observations = list(page.data)
        if any(o.name == "finalize" for o in observations):
            break
        await asyncio.sleep(2)
    names = {o.name for o in observations}
    assert {"chat-turn", "load-context", "finalize"} <= names, sorted(names)
    if final["outcome"] == "answered":
        generations = [o for o in observations if str(o.type).upper() == "GENERATION"]
        assert {"llm.understand", "llm.synthesize"} <= {g.name for g in generations}
