"""
Per-turn tracing: each graph node appends a structured step (persisted to
`app.traces` and shown in the UI trace viewer), emits a live status event
to the SSE stream, and runs inside an OpenTelemetry span.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from langgraph.config import get_stream_writer

from backend.agents.state import AgentState
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


NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def traced(node: str, status: str) -> Callable[[NodeFn], NodeFn]:
    """Decorator: status event + OTel span + timing for a graph node."""

    def deco(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        async def wrapper(state: AgentState) -> dict[str, Any]:
            emit({"type": "status", "node": node, "message": status})
            t0 = time.monotonic()
            with span(
                f"agent.{node}", turn_id=state.get("turn_id"), conversation_id=state.get("conversation_id")
            ):
                update = await fn(state)
            log.debug("node_complete", node=node, ms=round((time.monotonic() - t0) * 1000))
            return update

        return wrapper

    return deco


def append(state: AgentState, *steps: dict[str, Any]) -> list[dict[str, Any]]:
    return [*state.get("trace", []), *steps]
