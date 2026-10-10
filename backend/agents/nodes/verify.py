"""`verify` — output guardrail (grounded + complete), `replan` and `finalize`."""

from __future__ import annotations

import time
from typing import Any

from backend.agents.guardrails import FALLBACKS
from backend.agents.state import AgentState
from backend.agents.tools.base import UserContext
from backend.agents.trace import append, emit, step, traced
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import GUARDRAIL_BLOCKS
from backend.core.tokens import count_tokens
from backend.llm.client import get_llm
from backend.llm.prompts.schemas import Verification
from backend.llm.prompts.verify import build_verify_messages
from backend.llm.registry import Role

log = get_logger(__name__)
_VERIFY_HISTORY_TOKENS = 1_500


def _recent(history: str, budget: int = _VERIFY_HISTORY_TOKENS) -> str:
    """Keep the most recent part of the conversation within a token budget."""
    if count_tokens(history) <= budget:
        return history
    return history[-budget * 4 :]


@traced("verify", "Double-checking the answer")
async def verify(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    user = UserContext.from_dict(state["user"])
    messages = build_verify_messages(
        question=state.get("standalone_query") or state["message"],
        sources=state.get("sources_text", ""),
        answer=state.get("answer", ""),
        profile=user.profile_text(),
        history=_recent(state.get("recent_turns", "")),
    )
    try:
        v = await get_llm().structured(Role.FAST, messages, Verification, name="verify")
    except Exception as exc:  # a verifier outage must not block the answer
        log.warning("verification_skipped", error=str(exc)[:300])
        return {
            "verification": {"skipped": True, "error": exc.__class__.__name__},
            "trace": append(state, step("verify", output="skipped: verifier unavailable")),
        }
    verdict = v.model_dump()
    ms = (time.monotonic() - t0) * 1000
    trace = step(
        "verify",
        output={"grounded": v.grounded, "complete": v.complete},
        reasoning=v.reason,
        duration_ms=ms,
        unsupported_claims=v.unsupported_claims,
        missing=v.missing,
    )
    if not v.grounded:
        GUARDRAIL_BLOCKS.labels(reason="ungrounded").inc()
        log.info("answer_ungrounded", unsupported=v.unsupported_claims[:5])
        if settings.verify_replace_ungrounded:
            answer = FALLBACKS["insufficient_context"]
            emit({"type": "answer_replaced", "answer": answer, "reason": "insufficient_context"})
            return {
                "verification": verdict,
                "answer": answer,
                "outcome": "insufficient",
                "fallback": "insufficient_context",
                "citations": [],
                "trace": append(state, trace),
            }
    return {"verification": verdict, "trace": append(state, trace)}


@traced("replan", "Looking for the missing details")
async def replan(state: AgentState) -> dict[str, Any]:
    v = state.get("verification", {})
    feedback = "; ".join(
        [p for p in [v.get("missing", "")] if p]
        + [f"unsupported: {c}" for c in v.get("unsupported_claims", [])]
    )
    replans = state.get("replans", 0) + 1
    return {
        "replans": replans,
        "feedback": feedback,
        "trace": append(state, step("replan", output=feedback, round=replans)),
    }


@traced("finalize", "Finishing up")
async def finalize(state: AgentState) -> dict[str, Any]:
    outcome = state.get("outcome") or "answered"
    return {
        "outcome": outcome,
        "trace": append(
            state,
            step(
                "finalize",
                output=outcome,
                fallback=state.get("fallback"),
                replans=state.get("replans", 0),
                cache_hit=state.get("cache_hit", False),
            ),
        ),
    }
