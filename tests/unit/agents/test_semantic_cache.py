from __future__ import annotations

import pytest

from backend.agents.tools.base import UserContext
from backend.cache.semantic import (
    InMemorySemanticCache,
    cache_scope,
    cacheable,
    lookup_key,
    lookup_scopes,
    store_scope,
    ttl_for,
)
from backend.core.config import settings

MARIA = UserContext(
    user_id="u1",
    role="member",
    email="maria.garcia@demo.healthguard.dev",
    full_name="Maria Garcia",
    member_id="M100001",
    plan_id="PPO-SLV-PLS",
    plan_name="Choice PPO Silver Plus",
)
NO_PROFILE = UserContext(user_id="u2", role="member", email="new@x.dev", full_name="Sam Lee")
ADMIN = UserContext(user_id="u3", role="admin", email="admin@x.dev", full_name="Admin")


def test_scopes() -> None:
    assert cache_scope(ADMIN, needs_member_data=True) == "role:admin"
    assert cache_scope(MARIA, needs_member_data=True) == "member:M100001"
    assert cache_scope(MARIA, needs_member_data=False) == "public"
    assert cache_scope(NO_PROFILE, needs_member_data=True) is None
    assert cache_scope(NO_PROFILE, needs_member_data=False) == "public"


@pytest.mark.parametrize(
    "answer",
    [
        "Hi Maria, an EPO has no out-of-network coverage.",
        "Your Choice PPO Silver Plus plan covers it.",
        "Member M100001 can appeal.",
    ],
)
def test_public_answer_mentioning_identity_becomes_private(answer: str) -> None:
    assert store_scope("public", MARIA, answer) == "member:M100001"
    assert store_scope("public", NO_PROFILE, answer.replace("Maria", "Sam Lee")) in ("public", None)


def test_personalised_rewrite_makes_turn_private() -> None:
    rewrite = "How does external review work under my PPO-SLV-PLS plan?"
    assert store_scope("public", MARIA, "Generic answer.", rewrite) == "member:M100001"


def test_identity_answer_without_member_profile_is_not_cached() -> None:
    assert store_scope("public", NO_PROFILE, "Sam, appeals are due in 180 days.") is None
    assert store_scope("public", MARIA, "Appeals are due in 180 days.") == "public"


def test_lookup_key_uses_raw_message_without_context() -> None:
    state = {"message": "  How do   Appeals work? ", "standalone_query": "LLM rewrite"}
    assert lookup_key(state) == "how do appeals work?"
    assert lookup_key({**state, "recent_turns": "User: hi"}) == "LLM rewrite"


def test_members_read_their_own_scope_before_public() -> None:
    assert lookup_scopes(MARIA, needs_member_data=False) == ["member:M100001", "public"]
    assert lookup_scopes(MARIA, needs_member_data=True) == ["member:M100001"]
    assert lookup_scopes(NO_PROFILE, needs_member_data=False) == ["public"]
    assert lookup_scopes(NO_PROFILE, needs_member_data=True) == []
    assert lookup_scopes(ADMIN, needs_member_data=False) == ["role:admin"]


async def test_multi_scope_lookup() -> None:
    cache = InMemorySemanticCache()
    await cache.store("my question", {"answer": "mine"}, scope="member:M100001", data_version=1, ttl=60)
    hit = await cache.lookup("my question", scopes=["member:M100001", "public"], data_version=1)
    assert hit is not None and hit.payload["answer"] == "mine"
    assert await cache.lookup("my question", scopes=["member:M100002", "public"], data_version=1) is None


def test_ttl_by_scope() -> None:
    assert ttl_for("member:M1") == settings.semantic_cache_ttl_member_seconds
    assert ttl_for("public") == settings.semantic_cache_ttl_public_seconds


def _state(**kw: object) -> dict:
    base = {
        "outcome": "answered",
        "cache_hit": False,
        "cache_scope": "public",
        "verification": {"grounded": True, "complete": True},
        "results": [{"step_id": "s1", "tool": "sql", "task": "t", "ok": True}],
        "standalone_query": "q",
        "answer": "a",
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, True),
        ({"outcome": "insufficient"}, False),
        ({"cache_hit": True}, False),
        ({"cache_scope": ""}, False),
        ({"verification": {"skipped": True}}, False),
        ({"verification": {"grounded": True, "complete": False}}, False),
        ({"results": [{"step_id": "s1", "tool": "sql", "task": "t", "ok": False}]}, False),
        ({"results": []}, False),
    ],
)
def test_only_verified_successful_turns_are_cacheable(override: dict, expected: bool) -> None:
    assert cacheable(_state(**override)) is expected


async def test_in_memory_cache_filters_scope_version_and_ttl() -> None:
    cache = InMemorySemanticCache()
    await cache.store("what is a deductible", {"answer": "A"}, scope="public", data_version=3, ttl=60)
    hit = await cache.lookup("what is a deductible", scopes=["public"], data_version=3)
    assert hit is not None and hit.payload == {"answer": "A"} and hit.distance < 1e-6
    assert await cache.lookup("what is a deductible", scopes=["member:M100001"], data_version=3) is None
    assert await cache.lookup("what is a deductible", scopes=["public"], data_version=4) is None
    assert await cache.lookup("find a cardiologist in Austin", scopes=["public"], data_version=3) is None
    await cache.store("expired question", {"answer": "B"}, scope="public", data_version=3, ttl=-1)
    assert await cache.lookup("expired question", scopes=["public"], data_version=3) is None
