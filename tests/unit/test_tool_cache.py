"""Code-generating tools drop a cached first attempt that failed, so it is never replayed."""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents.tools.base import UserContext
from backend.agents.tools.dataframe_tool import run_dataframe_tool
from backend.agents.tools.sql_tool import run_sql_tool
from backend.core.config import settings
from backend.core.redis import get_redis
from backend.llm.client import LLMError, LLMResult, set_llm

KEY = "hg:llm:test-entry"
USER = UserContext(user_id="u1", role="member", email="m@example.com", full_name="Test Member")


class _CachedThenDown:
    """First call replays a cached (bad) output; the retry finds the model unavailable."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, *_: Any, **__: Any) -> LLMResult:
        self.calls += 1
        if self.calls == 1:
            return LLMResult(text=self.text, model="m", usage={}, cached=True, cache_key=KEY)
        raise LLMError("down")


@pytest.fixture(autouse=True)
def _exact_cache_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "llm_exact_cache_enabled", True)
    yield
    set_llm(None)


@pytest.mark.parametrize(
    ("tool", "bad_output"),
    [(run_sql_tool, "DROP TABLE claims"), (run_dataframe_tool, "import os\nresult = 1")],
)
async def test_rejected_cached_output_is_forgotten(tool: Any, bad_output: str) -> None:
    await get_redis().set(KEY, '{"text": "x", "model": "m"}')
    set_llm(_CachedThenDown(bad_output))
    result = await tool("s1", "task", USER, "")
    assert not result.ok and result.error_code == "llm_unavailable"
    assert not await get_redis().exists(KEY)
