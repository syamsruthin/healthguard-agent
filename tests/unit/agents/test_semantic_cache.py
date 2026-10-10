from __future__ import annotations

import pytest

from backend.agents.tools.base import UserContext
from backend.cache.semantic import (
    InMemorySemanticCache,
    cache_scope,
    cacheable,
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
    assert store_scope("public", answer, MARIA) == "member:M100001"
    assert store_scope("public", answer.replace("Maria", "Sam Lee"), NO_PROFILE) in ("public", None)


def test_identity_answer_without_member_profile_is_not_cached() -> None:
    assert store_scope("public", "Sam, appeals are due in 180 days.", NO_PROFILE) is None
    assert store_scope("public", "Appeals are due in 180 days.", MARIA) == "public"


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
    hit = await cache.lookup("what is a deductible", scope="public", data_version=3)
    assert hit is not None and hit.payload == {"answer": "A"} and hit.distance < 1e-6
    assert await cache.lookup("what is a deductible", scope="member:M100001", data_version=3) is None
    assert await cache.lookup("what is a deductible", scope="public", data_version=4) is None
    assert await cache.lookup("find a cardiologist in Austin", scope="public", data_version=3) is None
    await cache.store("expired question", {"answer": "B"}, scope="public", data_version=3, ttl=-1)
    assert await cache.lookup("expired question", scope="public", data_version=3) is None
