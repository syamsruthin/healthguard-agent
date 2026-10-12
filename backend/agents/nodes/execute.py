"""
`execute` — run the plan's dependency levels; steps within a level run concurrently.

Everything happens inside this one node (not as parallel LangGraph branches):
the state has no reducers, and a turn's results must be reset each turn.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.agents.guardrails import FALLBACKS
from backend.agents.planning import plan_levels
from backend.agents.sources import prior_context
from backend.agents.state import AgentState
from backend.agents.tools.base import ToolResult, UserContext
from backend.agents.tools.dataframe_tool import run_dataframe_tool
from backend.agents.tools.rag_tool import run_policy_search
from backend.agents.tools.sql_tool import run_sql_tool
from backend.agents.trace import append, emit, step, traced
from backend.core.config import settings
from backend.core.logging import get_logger

log = get_logger(__name__)

ToolFn = Callable[[str, str, UserContext, str], Awaitable[ToolResult]]
TOOLS: dict[str, ToolFn] = {
    "sql": run_sql_tool,
    "providers": run_dataframe_tool,
    "policy_search": run_policy_search,
}


def _failure(s: dict[str, Any], error: str, code: str) -> dict[str, Any]:
    return ToolResult(
        step_id=s["id"], tool=s["tool"], task=s["task"], ok=False, error=error, error_code=code
    ).as_dict()


async def _run_step(s: dict[str, Any], user: UserContext, done: list[dict[str, Any]]) -> dict[str, Any]:
    emit({"type": "tool", "step_id": s["id"], "tool": s["tool"], "task": s["task"], "status": "running"})
    ok_ids = {r["step_id"] for r in done if r.get("ok")}
    failed_deps = [d for d in s.get("depends_on", []) if d not in ok_ids]
    if failed_deps:
        out = _failure(s, f"skipped: dependency {', '.join(failed_deps)} failed", "dependency_failed")
    else:
        prior = prior_context(done, s["depends_on"]) if s.get("depends_on") else ""
        try:
            res = await asyncio.wait_for(
                TOOLS[s["tool"]](s["id"], s["task"], user, prior), timeout=settings.tool_step_timeout_seconds
            )
            out = res.as_dict()
        except TimeoutError:
            out = _failure(s, f"timed out after {settings.tool_step_timeout_seconds:.0f}s", "timeout")
        except Exception as exc:  # tools report failures in ToolResult; this is a last resort
            log.exception("tool_crashed", step=s["id"], tool=s["tool"])
            out = _failure(s, f"tool error: {exc.__class__.__name__}", "tool_error")
    out.pop("data", None)  # raw rows stay out of the checkpoint; `formatted` carries what synthesis needs
    emit(
        {
            "type": "tool",
            "step_id": s["id"],
            "tool": s["tool"],
            "task": s["task"],
            "status": "ok" if out["ok"] else "error",
            "row_count": out.get("row_count", 0),
            "duration_ms": out.get("duration_ms", 0),
            "attempts": out.get("attempts", 1),
            "error_code": out.get("error_code"),
        }
    )
    return out


@traced("execute", "Looking up the information")
async def execute(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    user = UserContext.from_dict(state["user"])
    results = list(state.get("results", []))  # earlier replan rounds stay available to synthesis
    round_results: list[dict[str, Any]] = []
    levels = plan_levels(state.get("plan", []))
    for level in levels:
        outs = await asyncio.gather(*(_run_step(s, user, results) for s in level))
        results.extend(outs)
        round_results.extend(outs)
    update: dict[str, Any] = {
        "results": results,
        "trace": append(
            state,
            step(
                "execute",
                input=[f"{s['id']}:{s['tool']}" for s in state.get("plan", [])],
                output=[
                    {
                        "step_id": r["step_id"],
                        "tool": r["tool"],
                        "ok": r["ok"],
                        "rows": r.get("row_count", 0),
                        "attempts": r.get("attempts", 1),
                        "error_code": r.get("error_code"),
                        "code": r.get("code", ""),
                    }
                    for r in round_results
                ],
                duration_ms=(time.monotonic() - t0) * 1000,
                levels=len(levels),
            ),
        ),
    }
    if round_results and not any(r["ok"] for r in results):
        reason = (
            "llm_unavailable"
            if any(r.get("error_code") == "llm_unavailable" for r in round_results)
            else "no_data"
        )
        update.update(outcome="error", fallback=reason, answer=FALLBACKS[reason])
    return update
