"""Integration tests need `docker compose up -d postgres redis` plus `make data` (or `hg-seed --scale small`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from backend.agents.tools import sandbox
from backend.core.redis import close_redis
from backend.db.session import SessionLocal, dispose

_TEST_TURNS = """
    SELECT m.trace_id FROM app.messages m
    JOIN app.conversations c ON c.id = m.conversation_id JOIN app.users u ON u.id = m.user_id
    WHERE u.email LIKE '%@example.com' OR c.title = 'pytest'
"""


async def _delete_test_data() -> None:
    """Remove what tests created: example.com users, 'pytest' conversations on persona accounts,
    their traces and LLM-call rows, and background calls recorded by the scripted FakeLLM."""
    async with SessionLocal() as session:
        await session.execute(text(f"DELETE FROM app.llm_calls WHERE trace_id IN ({_TEST_TURNS})"))
        await session.execute(text(f"DELETE FROM app.traces WHERE id IN ({_TEST_TURNS})"))
        # FakeLLM records exactly 50 completion tokens at zero cost and zero latency.
        await session.execute(
            text(
                "DELETE FROM app.llm_calls WHERE trace_id IS NULL AND completion_tokens = 50 "
                "AND cost_usd = 0 AND latency_ms = 0"
            )
        )
        await session.execute(text("DELETE FROM app.conversations WHERE title = 'pytest'"))
        await session.execute(text("DELETE FROM app.users WHERE email LIKE '%@example.com'"))
        await session.commit()


@pytest.fixture(scope="session", autouse=True)
async def _close_shared_resources() -> AsyncIterator[None]:
    yield
    await _delete_test_data()
    sandbox.shutdown()
    await close_redis()
    await dispose()
