"""
Per-turn tracing: each graph node appends a structured step (persisted to
`app.traces` and shown in the UI trace viewer), emits a live status event
to the SSE stream, and runs inside an OpenTelemetry span and a Langfuse
observation. The Langfuse observation mirrors the node's step (input, output,
reasoning, metadata), so the database trace and Langfuse never disagree.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Protocol

from langgraph.config import get_stream_writer

from backend.agents.state import AgentState
from backend.core import observability as obs
from backend.core.logging import get_logger
from backend.core.telemetry import span

log = get_logger(__name__)


def emit(event: dict[str, Any]) -> None:
    """Send a custom event to the stream (no-op when not streaming)."""
    try:
        get_stream_writer()(event)
    except Exception:  # outside a streaming run
        log.debug("stream_writer_unavailable", event_type=event.get("type"))


def step(
    node: str,
    *,
    input: Any = "",
    output: Any = "",
    reasoning: str = "",
    duration_ms: float = 0,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "node": node,
        "at": datetime.now(UTC).isoformat(),
        "input": input if isinstance(input, str | int | float | bool | list | dict) else str(input),
        "output": output if isinstance(output, str | int | float | bool | list | dict) else str(output),
        "reasoning": reasoning,
        "duration_ms": round(duration_ms, 1),
        "metadata": {k: v for k, v in metadata.items() if v not in (None, "", [], {})},
    }


class NodeFn(Protocol):
    """A graph node; the parameter must be named `state` to match LangGraph's node protocol."""

    def __call__(self, state: AgentState) -> Awaitable[dict[str, Any]]: ...


# Routing-relevant fields a node may set, copied onto its Langfuse observation.
_ROUTING_KEYS = ("outcome", "fallback", "block_reason", "cache_hit", "revise", "revisions", "replans")


def observe_node(o: obs.Observation, state: AgentState, update: dict[str, Any]) -> None:
    """Copy the step(s) a node appended, and its routing decisions, onto its observation."""
    new_steps = update.get("trace", [])[len(state.get("trace", [])) :]
    routing = {k: update[k] for k in _ROUTING_KEYS if k in update}
    if not new_steps:
        o.update(metadata=routing)
        return
    last = new_steps[-1]
    o.update(
        input=last.get("input") or None,
        output=last.get("output") or None,
        metadata={
            "reasoning": last.get("reasoning"),
            "duration_ms": last.get("duration_ms"),
            **last.get("metadata", {}),
            **routing,
        },
    )
    if update.get("outcome") == "error":
        o.fail(f"fallback: {update.get('fallback') or 'error'}")
    elif update.get("outcome") in ("blocked", "insufficient"):
        o.update(level="WARNING", status_message=f"{update['outcome']}: {update.get('fallback', '')}")


def traced(node: str, status: str, *, as_type: obs.ObsType = "span") -> Callable[[NodeFn], NodeFn]:
    """Decorator: status event + OTel span + Langfuse observation + timing for a graph node."""

    def deco(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        async def wrapper(state: AgentState) -> dict[str, Any]:
            emit({"type": "status", "node": node, "message": status})
            t0 = time.monotonic()
            with (
                obs.observe(node, as_type=as_type, metadata={"round": state.get("replans", 0)}) as o,
                span(
                    f"agent.{node}",
                    turn_id=state.get("turn_id"),
                    conversation_id=state.get("conversation_id"),
                ),
            ):
                update = await fn(state)
                observe_node(o, state, update)
            log.debug("node_complete", node=node, ms=round((time.monotonic() - t0) * 1000))
            return update

        return wrapper

    return deco


def append(state: AgentState, *steps: dict[str, Any]) -> list[dict[str, Any]]:
    return [*state.get("trace", []), *steps]


def synthesis_sources(steps: list[dict[str, Any]]) -> str:
    """The labelled sources the final answer was written from (the last synthesis step's)."""
    for s in reversed(steps):
        if s.get("node") == "synthesize":
            return str(s.get("metadata", {}).get("sources", ""))
    return ""
