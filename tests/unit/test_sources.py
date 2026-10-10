from __future__ import annotations

from backend.agents.sources import build_sources, prior_context

CHUNK = {
    "chunk_id": "c1",
    "doc_id": "claims",
    "title": "Claims Guide",
    "section_path": "Claims Guide › Filing",
    "content": "File within 180 days.",
}
RESULTS = [
    {
        "step_id": "s1",
        "tool": "sql",
        "task": "my deductible",
        "ok": True,
        "formatted": "| met | 2700 |",
        "code": "SELECT 1",
        "row_count": 1,
    },
    {
        "step_id": "s2",
        "tool": "policy_search",
        "task": "claim deadline",
        "ok": True,
        "chunks": [CHUNK, CHUNK],
    },
    {"step_id": "s3", "tool": "providers", "task": "cardiologists", "ok": False, "error": "boom"},
]


def test_tags_and_citations() -> None:
    text, citations = build_sources(RESULTS)
    assert "[S1] Insurance database — my deductible" in text
    assert "[D1] Policy document — Claims Guide › Filing" in text
    assert "[D2]" not in text  # duplicate chunk deduplicated
    assert "[S2] Provider directory — lookup FAILED" in text
    assert [c["tag"] for c in citations] == ["S1", "D1"]
    assert citations[1]["chunk_id"] == "c1"


def test_prior_context_only_includes_successful_dependencies() -> None:
    ctx = prior_context(RESULTS, ["s1", "s3"])
    assert "From step s1" in ctx and "2700" in ctx
    assert "s3" not in ctx
