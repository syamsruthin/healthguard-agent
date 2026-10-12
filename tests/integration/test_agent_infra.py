"""Agent infrastructure against real Postgres (checkpointer) and Redis 8 (vector search)."""

from __future__ import annotations

import uuid

from backend.agents.graph import build_graph, open_checkpointer
from backend.agents.runner import run_graph
from backend.agents.state import initial_state
from backend.agents.tools.base import UserContext
from backend.cache.semantic import RedisSemanticCache

USER = UserContext(user_id="u", role="member", email="x@y.z", full_name="Test User", member_id="M100001")


async def test_postgres_checkpointer_roundtrip_and_delete(fake_llm) -> None:
    fake_llm(
        {
            "understand": {
                "in_scope": True,
                "standalone_query": "hi",
                "needs_member_data": False,
                "plan": [],
                "reason": "greeting",
            },
            "synthesize": "Hello!",
        }
    )
    pool, saver = await open_checkpointer(max_size=2)
    try:
        graph = build_graph(saver)
        thread = str(uuid.uuid4())
        state = initial_state(turn_id="t", conversation_id=thread, user=USER.as_dict(), message="hi")
        final = await run_graph(graph, state, thread_id=thread)
        assert final["answer"] == "Hello!"
        config = {"configurable": {"thread_id": thread}}
        snapshot = await graph.aget_state(config)
        assert snapshot.values["outcome"] == "smalltalk"
        await saver.adelete_thread(thread)
        assert (await graph.aget_state(config)).values == {}
    finally:
        await pool.close()


async def test_redis_semantic_cache_roundtrip_and_isolation() -> None:
    cache = RedisSemanticCache()
    version = 900_000 + uuid.uuid4().int % 99_999  # isolates this run from real entries
    try:
        await cache.store(
            "how do I appeal a denied claim", {"answer": "A"}, scope="public", data_version=version, ttl=60
        )
        hit = await cache.lookup("how do I appeal a denied claim", scopes=["public"], data_version=version)
        assert (
            hit is not None
            and hit.payload == {"answer": "A"}
            and hit.distance < 0.01
            and hit.scope == "public"
        )
        multi = await cache.lookup(
            "how do I appeal a denied claim", scopes=["member:M100001", "public"], data_version=version
        )
        assert multi is not None and multi.payload == {"answer": "A"} and multi.scope == "public"
        assert (
            await cache.lookup(
                "how do I appeal a denied claim", scopes=["member:M100001"], data_version=version
            )
            is None
        )
        assert (
            await cache.lookup("how do I appeal a denied claim", scopes=["public"], data_version=version + 1)
            is None
        )
        assert (
            await cache.lookup("find a dermatologist in Seattle", scopes=["public"], data_version=version)
            is None
        )
    finally:
        await cache.close()
