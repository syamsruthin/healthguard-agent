"""The conversation turn lock is released only by the turn that holds it."""

from __future__ import annotations

from backend.core.config import settings
from backend.core.redis import get_redis
from backend.services.chat_service import lock_seconds, release_lock


async def test_only_the_owner_releases_the_lock() -> None:
    key = "hg:turn:test"
    await get_redis().set(key, "turn-b")
    assert await release_lock(key, "turn-a") is False  # an older turn whose lock had expired
    assert await get_redis().get(key) == "turn-b"
    assert await release_lock(key, "turn-b") is True
    assert not await get_redis().exists(key)


async def test_release_of_a_missing_lock_is_harmless() -> None:
    assert await release_lock("hg:turn:none", "turn-a") is False


def test_lock_outlives_the_turn_deadline() -> None:
    assert lock_seconds() > settings.turn_timeout_seconds
