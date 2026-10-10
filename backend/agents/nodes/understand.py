"""`understand` — deterministic prefilter, then one FAST call: scope + safety + rewrite + plan."""

from __future__ import annotations

import time
from typing import Any

from backend.agents.guardrails import FALLBACKS, prefilter
from backend.agents.planning import sanitize_plan
from backend.agents.state import AgentState
from backend.agents.tools.base import UserContext
from backend.agents.trace import append, emit, step, traced
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import GUARDRAIL_BLOCKS
from backend.llm.client import get_llm
from backend.llm.prompts.schemas import Understanding
from backend.llm.prompts.understand import build_understand_messages
from backend.llm.registry import Role

log = get_logger(__name__)


def _ms(t0: float) -> float:
    return (time.monotonic() - t0) * 1000


def _block(state: AgentState, reason: str, t0: float, *, layer: str, rationale: str = "") -> dict[str, Any]:
    GUARDRAIL_BLOCKS.labels(reason=reason).inc()
    return {
        "outcome": "blocked",
        "block_reason": reason,
        "fallback": reason,
        "answer": FALLBACKS[reason],
        "trace": append(
            state,
            step(
                "understand",
                input=state["message"],
                output=f"blocked: {reason}",
                reasoning=rationale,
                duration_ms=_ms(t0),
                layer=layer,
            ),
        ),
    }


@traced("understand", "Understanding your question")
async def understand(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    user = UserContext.from_dict(state["user"])
    replans = state.get("replans", 0)
    message = state["message"]

    if not replans and (reason := prefilter(message, user)):
        return _block(state, reason, t0, layer="prefilter")

    messages = build_understand_messages(
        profile=user.profile_text(),
        message=message,
        recent_turns=state.get("recent_turns", ""),
        summary=state.get("summary", ""),
        memories=state.get("memories", ""),
        feedback=state.get("feedback", ""),
    )
    try:
        u = await get_llm().structured(
            Role.FAST, messages, Understanding, name="understand.replan" if replans else "understand"
        )
    except Exception as exc:
        log.warning("understand_failed", error=str(exc)[:300], replans=replans)
        note = step("understand", input=message, output=f"model unavailable: {exc.__class__.__name__}")
        if replans:  # keep the answer we already have
            return {"plan": [], "trace": append(state, note)}
        return {
            "outcome": "error",
            "fallback": "llm_unavailable",
            "answer": FALLBACKS["llm_unavailable"],
            "trace": append(state, note),
        }

    reason = u.safety if u.safety != "ok" else (None if u.in_scope else "out_of_scope")
    if reason and not replans:
        return _block(state, reason, t0, layer="llm", rationale=u.reason)

    plan, warnings = (
        ([], [])
        if reason
        else sanitize_plan(
            [s.model_dump() for s in u.plan],
            max_steps=settings.max_plan_steps,
            prefix=f"r{replans}-" if replans else "",
        )
    )
    update: dict[str, Any] = {"understanding": u.model_dump(), "plan": plan}
    if not replans:
        update["standalone_query"] = u.standalone_query.strip() or message
        update["needs_member_data"] = u.needs_member_data
        if not plan:
            update["outcome"] = "smalltalk"
    if plan:
        emit(
            {
                "type": "plan",
                "round": replans,
                "standalone_query": update.get("standalone_query", state.get("standalone_query", "")),
                "steps": plan,
            }
        )
    update["trace"] = append(
        state,
        step(
            "understand",
            input=message,
            output={"standalone_query": u.standalone_query, "plan": plan},
            reasoning=u.reason,
            duration_ms=_ms(t0),
            round=replans,
            needs_member_data=u.needs_member_data,
            warnings=warnings,
        ),
    )
    return update
