"""
The HealthGuard agent graph (LangGraph).

    START → understand ─┬─ blocked / error ───────────────────────────────→ finalize → END
                        ├─ small talk (empty plan) → synthesize ──────────→ finalize
                        └─ cache_lookup ─┬─ hit ──────────────────────────→ finalize
                                         └─ execute → synthesize → verify ┬→ finalize
                                                                          ├→ synthesize (rewrite without
                                                                          │  unsupported claims, once)
                                                                          └→ replan → understand → execute …

`thread_id` is the conversation id. Every turn starts from `initial_state()`,
so the checkpoint never leaks one turn's results into the next.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from backend.agents.nodes import cache_lookup, execute, finalize, replan, synthesize, understand, verify
from backend.agents.state import AgentState
from backend.core.config import settings


def route_after_understand(state: AgentState) -> str:
    if state.get("outcome") in ("blocked", "error"):
        return "finalize"
    if state.get("replans"):
        return "execute" if state.get("plan") else "finalize"
    if not state.get("plan"):
        return "synthesize"
    return "cache_lookup"


def route_after_cache(state: AgentState) -> str:
    return "finalize" if state.get("cache_hit") else "execute"


def route_after_execute(state: AgentState) -> str:
    if state.get("fallback"):
        return "finalize"
    replans = state.get("replans", 0)
    if replans:
        prefix = f"r{replans}-"
        if not any(r["ok"] for r in state.get("results", []) if r["step_id"].startswith(prefix)):
            return "finalize"  # nothing new was found; keep the previous answer
    return "synthesize"


def route_after_synthesize(state: AgentState) -> str:
    if state.get("outcome") in ("smalltalk", "error") or state.get("verification", {}).get("skipped"):
        return "finalize"
    return "verify"


def route_after_verify(state: AgentState) -> str:
    v = state.get("verification", {})
    if v.get("skipped") or state.get("outcome") == "insufficient":
        return "finalize"
    if state.get("revise"):
        return "synthesize"
    if not v.get("complete", True) and v.get("missing") and state.get("replans", 0) < settings.max_replans:
        return "replan"
    return "finalize"


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(AgentState)
    g.add_node("understand", understand)
    g.add_node("cache_lookup", cache_lookup)
    g.add_node("execute", execute)
    g.add_node("synthesize", synthesize)
    g.add_node("verify", verify)
    g.add_node("replan", replan)
    g.add_node("finalize", finalize)

    g.add_edge(START, "understand")
    g.add_conditional_edges(
        "understand", route_after_understand, ["finalize", "synthesize", "cache_lookup", "execute"]
    )
    g.add_conditional_edges("cache_lookup", route_after_cache, ["finalize", "execute"])
    g.add_conditional_edges("execute", route_after_execute, ["finalize", "synthesize"])
    g.add_conditional_edges("synthesize", route_after_synthesize, ["finalize", "verify"])
    g.add_conditional_edges("verify", route_after_verify, ["finalize", "replan", "synthesize"])
    g.add_edge("replan", "understand")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


async def open_checkpointer(max_size: int = 10) -> tuple[AsyncConnectionPool, AsyncPostgresSaver]:
    """Postgres checkpointer on its own psycopg pool (the app's SQLAlchemy engine uses asyncpg)."""
    pool: AsyncConnectionPool = AsyncConnectionPool(
        conninfo=settings.sync_database_url,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await saver.setup()
    return pool, saver


_graph: CompiledStateGraph[Any, Any, Any, Any] | None = None


def get_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """The process-wide graph; the API installs a Postgres-checkpointed one at startup."""
    global _graph
    if _graph is None:
        _graph = build_graph(InMemorySaver())
    return _graph


def set_graph(graph: CompiledStateGraph[Any, Any, Any, Any] | None) -> None:
    global _graph
    _graph = graph
