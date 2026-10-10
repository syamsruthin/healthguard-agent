"""LangGraph state for one chat turn (JSON-serialisable for the Postgres checkpointer)."""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # ── Inputs (set by the chat service for every turn) ──
    turn_id: str
    conversation_id: str
    user: dict[str, Any]  # UserContext.as_dict()
    message: str
    recent_turns: str
    summary: str
    memories: str
    data_version: int

    # ── Understanding / planning ──
    understanding: dict[str, Any]
    standalone_query: str
    needs_member_data: bool
    plan: list[dict[str, Any]]
    replans: int
    feedback: str
    revisions: int  # rewrites requested by the verifier
    revise: bool  # a rewrite is pending
    revision_notes: str

    # ── Execution ──
    results: list[dict[str, Any]]  # ToolResult.as_dict()
    sources_text: str
    citations: list[dict[str, Any]]

    # ── Output ──
    answer: str
    verification: dict[str, Any]
    outcome: str  # answered | smalltalk | blocked | cache_hit | insufficient | error
    fallback: str  # FALLBACKS key used for the final answer, if any
    block_reason: str
    cache_hit: bool
    cache_scope: str
    cache_query: str  # text the semantic cache was keyed on
    trace: list[dict[str, Any]]


def initial_state(
    *,
    turn_id: str,
    conversation_id: str,
    user: dict[str, Any],
    message: str,
    recent_turns: str = "",
    summary: str = "",
    memories: str = "",
    data_version: int = 0,
) -> AgentState:
    """Full input state — every key is reset so nothing leaks from the previous checkpointed turn."""
    return AgentState(
        turn_id=turn_id,
        conversation_id=conversation_id,
        user=user,
        message=message,
        recent_turns=recent_turns,
        summary=summary,
        memories=memories,
        data_version=data_version,
        understanding={},
        standalone_query="",
        needs_member_data=False,
        plan=[],
        replans=0,
        feedback="",
        revisions=0,
        revise=False,
        revision_notes="",
        results=[],
        sources_text="",
        citations=[],
        answer="",
        verification={},
        outcome="",
        fallback="",
        block_reason="",
        cache_hit=False,
        cache_scope="",
        cache_query="",
        trace=[],
    )
