"""Integration tests need `docker compose up -d postgres redis` plus `make data` (or `hg-seed --scale small`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from backend.agents.tools import sandbox
from backend.core.redis import close_redis
from backend.db.session import dispose


@pytest.fixture(scope="session", autouse=True)
async def _close_shared_resources() -> AsyncIterator[None]:
    yield
    sandbox.shutdown()
    await close_redis()
    await dispose()
