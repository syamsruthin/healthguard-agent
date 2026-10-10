"""Integration tests need `docker compose up -d postgres redis` plus `make data` (or `hg-seed --scale small`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from backend.agents.tools import sandbox
from backend.core.redis import close_redis
from backend.db.session import SessionLocal, dispose


async def _delete_test_data() -> None:
    """Remove users created by tests (example.com addresses) and their traces / LLM calls."""
    async with SessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM app.llm_calls WHERE trace_id IN (SELECT m.trace_id FROM app.messages m "
                "JOIN app.users u ON u.id = m.user_id WHERE u.email LIKE '%@example.com')"
            )
        )
        await session.execute(
            text(
                "DELETE FROM app.traces WHERE user_id IN (SELECT id FROM app.users WHERE email LIKE '%@example.com')"
            )
        )
        await session.execute(text("DELETE FROM app.users WHERE email LIKE '%@example.com'"))
        await session.commit()


@pytest.fixture(scope="session", autouse=True)
async def _close_shared_resources() -> AsyncIterator[None]:
    yield
    await _delete_test_data()
    sandbox.shutdown()
    await close_redis()
    await dispose()
