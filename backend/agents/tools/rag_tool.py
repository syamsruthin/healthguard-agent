"""`policy_search` tool — hybrid retrieval over policy documents and the member's SBC."""

from __future__ import annotations

import time
from dataclasses import asdict

from backend.agents.tools.base import ToolResult, UserContext
from backend.core.telemetry import TOOL_EVENTS
from backend.rag.retriever import hybrid_search


async def run_policy_search(step_id: str, task: str, user: UserContext, prior: str = "") -> ToolResult:
    t0 = time.monotonic()
    query = task if not prior else f"{task}\n\nContext: {prior[:500]}"
    try:
        hits = await hybrid_search(query, plan_id=user.plan_id)
    except Exception as exc:
        TOOL_EVENTS.labels(tool="policy_search", outcome="error").inc()
        return ToolResult(
            step_id=step_id,
            tool="policy_search",
            task=task,
            ok=False,
            error=f"Retrieval failed: {exc.__class__.__name__}",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    TOOL_EVENTS.labels(tool="policy_search", outcome="ok" if hits else "empty").inc()
    chunks = [{k: v for k, v in asdict(h).items() if k != "extra"} for h in hits]
    formatted = "\n\n".join(f"{h.section_path}\n{h.content}" for h in hits) or "(no relevant passages found)"
    return ToolResult(
        step_id=step_id,
        tool="policy_search",
        task=task,
        ok=True,
        chunks=chunks,
        row_count=len(hits),
        formatted=formatted,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
