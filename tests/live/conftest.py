"""Live tests: `HG_LIVE=1 pytest -m live` (real OpenRouter models, seeded + ingested database)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from backend.agents.tools import sandbox
from backend.core.redis import close_redis
from backend.db.session import dispose
from tests.integration.api.conftest import client, jobs  # noqa: F401  (re-exported fixtures)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("HG_LIVE") != "1":
        skip = pytest.mark.skip(reason="set HG_LIVE=1 (and OPENROUTER_API_KEY) to run live tests")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
async def _close_shared_resources() -> AsyncIterator[None]:
    yield
    sandbox.shutdown()
    await close_redis()
    await dispose()
