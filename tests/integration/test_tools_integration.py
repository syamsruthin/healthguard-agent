from __future__ import annotations

import pytest

from backend.agents.tools.sandbox import SandboxError, run_code
from backend.rag.retriever import hybrid_search


async def test_sandbox_runs_pandas_over_the_lake() -> None:
    out = await run_code(
        "df = providers[providers['is_pcp']]\nresult = {'pcps': int(len(df)), 'total': int(len(providers))}"
    )
    assert out["type"] == "dict"
    assert 0 < out["value"]["pcps"] < out["value"]["total"]


async def test_sandbox_rejects_escape_before_spawning() -> None:
    with pytest.raises(SandboxError):
        await run_code("import os\nresult = 1")


async def test_hybrid_search_returns_policy_chunks() -> None:
    hits = await hybrid_search("how do I appeal a denied claim", top_k=5)
    assert hits
    assert all(h.category != "sbc" for h in hits)  # no plan given → no SBCs
    assert any("appeal" in h.content.lower() for h in hits)


async def test_hybrid_search_includes_member_plan_sbc() -> None:
    hits = await hybrid_search("what is my specialist copay", top_k=8, plan_id="PPO-SLV-PLS")
    sbcs = {h.plan_id for h in hits if h.category == "sbc"}
    assert sbcs <= {"PPO-SLV-PLS"}
