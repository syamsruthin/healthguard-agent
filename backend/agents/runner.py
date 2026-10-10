"""Run one agent turn, forwarding live events and returning the final state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from backend.agents.state import AgentState

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


async def run_graph(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    state: AgentState,
    *,
    thread_id: str,
    sink: EventSink | None = None,
) -> dict[str, Any]:
    """Stream the graph: custom events go to `sink`; node updates fold into the returned state."""
    final: dict[str, Any] = dict(state)
    config: Any = {"configurable": {"thread_id": thread_id}}
    stream: Any = graph.astream(state, config, stream_mode=["custom", "updates"], durability="exit")
    async for mode, chunk in stream:
        if mode == "custom":
            if sink is not None:
                await sink(chunk)
        elif mode == "updates":
            for update in chunk.values():
                if isinstance(update, dict):
                    final.update(update)
    return final
