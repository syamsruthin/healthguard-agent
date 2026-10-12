"""`synthesize` — stream a grounded, cited answer from the labelled sources."""

from __future__ import annotations

import re
import time
from typing import Any

from backend.agents.guardrails import FALLBACKS
from backend.agents.sources import build_sources
from backend.agents.state import AgentState
from backend.agents.tools.base import UserContext
from backend.agents.trace import append, emit, step, traced
from backend.core.logging import get_logger
from backend.llm.client import get_llm
from backend.llm.prompts.synthesize import build_synthesis_messages
from backend.llm.registry import Role

log = get_logger(__name__)
_TAG = re.compile(r"\[([SD]\d+)\]")
_TRACE_SOURCES_CHARS = 12_000


def cited(answer: str, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Citations whose tag appears in the answer (all of them if the model cited nothing)."""
    used = set(_TAG.findall(answer))
    return [c for c in citations if c["tag"] in used] if used else citations


@traced("synthesize", "Writing the answer")
async def synthesize(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    user = UserContext.from_dict(state["user"])
    replans = state.get("replans", 0)
    no_tools = state.get("outcome") == "smalltalk"
    sources, citations = ("", []) if no_tools else build_sources(state.get("results", []))
    question = state["message"] if no_tools else (state.get("standalone_query") or state["message"])
    messages = build_synthesis_messages(
        profile=user.profile_text(),
        question=question,
        sources=sources,
        history=state.get("recent_turns", ""),
        summary=state.get("summary", ""),
        memories=state.get("memories", ""),
        no_tools=no_tools,
        revision_notes=state.get("revision_notes", "") if state.get("revise") else "",
    )
    if state.get("revise"):
        emit({"type": "answer_reset", "round": replans, "reason": "revision"})
    elif replans:
        emit({"type": "answer_reset", "round": replans, "reason": "replan"})
    parts: list[str] = []
    try:
        async for chunk in get_llm().stream(Role.SYNTH, messages, name="synthesize"):
            parts.append(chunk)
            emit({"type": "token", "text": chunk})
    except Exception as exc:
        log.warning("synthesis_failed", error=str(exc)[:300], replans=replans)
        note = step("synthesize", output=f"model unavailable: {exc.__class__.__name__}")
        if (replans or state.get("revise")) and state.get("answer"):  # restore the previous answer
            emit({"type": "answer_replaced", "answer": state["answer"], "reason": "llm_unavailable"})
            return {
                "verification": {**state.get("verification", {}), "skipped": True},
                "revise": False,
                "trace": append(state, note),
            }
        answer = FALLBACKS["llm_unavailable"]
        emit({"type": "answer_replaced", "answer": answer, "reason": "llm_unavailable"})
        return {
            "answer": answer,
            "outcome": "error",
            "fallback": "llm_unavailable",
            "citations": [],
            "trace": append(state, note),
        }
    answer = "".join(parts).strip()
    used = cited(answer, citations)
    emit({"type": "citations", "citations": used})
    return {
        "answer": answer,
        "sources_text": sources,
        "citations": used,
        "outcome": "smalltalk" if no_tools else "answered",
        "revise": False,
        "trace": append(
            state,
            step(
                "synthesize",
                input=question,
                output=answer,
                duration_ms=(time.monotonic() - t0) * 1000,
                round=replans,
                sources=sources[:_TRACE_SOURCES_CHARS],
                citations=[c["tag"] for c in used],
            ),
        ),
    }
