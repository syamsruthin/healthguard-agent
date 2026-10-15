"""
One chat turn, end to end.

HTTP phase (`prepare_turn`, errors become real status codes):
  input checks → ownership → per-conversation turn lock (owned by the turn id,
  outliving the turn deadline) → persist the user message.

Stream phase (`execute_turn`, runs in a background task so a client
disconnect never cancels persistence):
  meta event → usage collection → user / memory context → agent graph
  (events forwarded live, bounded by TURN_TIMEOUT_SECONDS) → persist assistant
  message, trace and LLM calls →
  final event → semantic-cache store → follow-up jobs.

The whole stream phase is one Langfuse trace (`chat-turn`, id = the turn's
`trace_id`, session = the conversation), scored with the turn's outcome.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from redis.exceptions import WatchError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse

from backend.agents.graph import get_graph
from backend.agents.guardrails import FALLBACKS
from backend.agents.runner import run_graph
from backend.agents.state import initial_state
from backend.agents.tools.base import UserContext
from backend.cache.semantic import maybe_store
from backend.core import observability as obs
from backend.core.config import settings
from backend.core.errors import AppError, ConflictError
from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.core.telemetry import TURN_LATENCY
from backend.db.base import utcnow
from backend.db.models import Conversation, Message, Trace, User
from backend.db.session import SessionLocal, get_data_version
from backend.llm.prompts import PROMPT_VERSION
from backend.llm.usage import UsageCollector, start_collection, stop_collection
from backend.memory import long_term
from backend.memory.context import MemoryContext, build_context
from backend.services import conversation_service
from backend.services.llm_calls import llm_call_rows
from backend.services.user_context import load_user_context
from backend.workers.queue import get_enqueuer

log = get_logger(__name__)
EventSink = Callable[[dict[str, Any]], Awaitable[None]]
_background: set[asyncio.Task[None]] = set()


@dataclass
class PreparedTurn:
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    message: str
    turn_id: str
    first_turn: bool
    lock_key: str
    started: float
    role: str = "member"


async def _noop(_: dict[str, Any]) -> None:
    return None


def lock_seconds() -> int:
    """The turn lock outlives the turn deadline, so it can only expire if the process died."""
    return int(settings.turn_timeout_seconds) + 30


async def prepare_turn(
    session: AsyncSession, user: User, conversation_id: uuid.UUID, message: str
) -> PreparedTurn:
    message = message.strip()
    if not message:
        raise AppError("Message is empty", code="empty_message")
    if len(message) > settings.max_input_chars:
        raise AppError(
            f"Message is longer than {settings.max_input_chars} characters", code="message_too_long"
        )
    conversation = await conversation_service.get_owned(session, user, conversation_id)
    lock_key = f"hg:turn:{conversation.id}"
    turn_id = str(uuid.uuid4())
    if not await get_redis().set(lock_key, turn_id, nx=True, ex=lock_seconds()):
        raise ConflictError("A reply is still being generated for this conversation", code="turn_in_progress")
    try:
        first_turn = await conversation_service.message_count(session, conversation.id) == 0
        user_msg = Message(conversation_id=conversation.id, user_id=user.id, role="user", content=message)
        session.add(user_msg)
        conversation.updated_at = utcnow()
        await session.commit()
    except Exception:
        await release_lock(lock_key, turn_id)
        raise
    return PreparedTurn(
        user_id=user.id,
        conversation_id=conversation.id,
        user_message_id=user_msg.id,
        message=message,
        turn_id=turn_id,
        first_turn=first_turn,
        lock_key=lock_key,
        started=time.monotonic(),
        role=user.role,
    )


async def release_lock(lock_key: str, owner: str) -> bool:
    """Delete the turn lock only if this turn still owns it (compare-and-delete under WATCH)."""
    try:
        async with get_redis().pipeline() as pipe:
            await pipe.watch(lock_key)
            if await pipe.get(lock_key) != owner:
                await pipe.unwatch()
                return False
            pipe.multi()
            pipe.delete(lock_key)
            await pipe.execute()
            return True
    except WatchError:  # the lock changed hands while we checked
        return False
    except Exception as exc:
        log.warning("turn_lock_release_failed", error=str(exc)[:200])
        return False


async def release(prepared: PreparedTurn) -> None:
    await release_lock(prepared.lock_key, prepared.turn_id)


def member_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool summaries without the generated SQL / pandas code (internal detail, admin-only)."""
    return [{k: v for k, v in t.items() if k != "code"} for t in tools]


def member_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Citations without the code behind a data source (document snippets stay)."""
    return [{k: v for k, v in c.items() if k != "detail"} for c in citations]


def _member_sink(sink: EventSink) -> EventSink:
    """Live events for a member: streamed citations lose their code detail."""

    async def forward(event: dict[str, Any]) -> None:
        if event.get("type") == "citations":
            event = {**event, "citations": member_citations(event.get("citations", []))}
        await sink(event)

    return forward


def _tool_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "step_id": r["step_id"],
            "tool": r["tool"],
            "task": r["task"],
            "ok": r.get("ok", False),
            "row_count": r.get("row_count", 0),
            "attempts": r.get("attempts", 1),
            "duration_ms": r.get("duration_ms", 0),
            "error_code": r.get("error_code"),
            "code": r.get("code", ""),
        }
        for r in results
    ]


async def _persist(
    prepared: PreparedTurn,
    final: dict[str, Any],
    collector: UsageCollector,
    *,
    trace_id: uuid.UUID,
    latency_ms: int,
) -> uuid.UUID:
    totals = collector.totals
    async with SessionLocal() as session:
        msg = Message(
            conversation_id=prepared.conversation_id,
            user_id=prepared.user_id,
            role="assistant",
            content=final.get("answer", ""),
            standalone_query=final.get("standalone_query") or None,
            trace_id=trace_id,
            tools=_tool_summaries(final.get("results", [])),
            citations=final.get("citations", []),
            meta={
                "outcome": final.get("outcome"),
                "fallback": final.get("fallback") or None,
                "verification": final.get("verification") or {},
                "replans": final.get("replans", 0),
                "revisions": final.get("revisions", 0),
                "cache_scope": final.get("cache_scope") or None,
                "turn_id": prepared.turn_id,
                "plan": final.get("plan", []),
                "usage": totals,
            },
            latency_ms=latency_ms,
            prompt_tokens=int(totals["prompt_tokens"]),
            completion_tokens=int(totals["completion_tokens"]),
            cached_tokens=int(totals["cached_tokens"]),
            cost_usd=Decimal(str(totals["cost_usd"])),
            cache_hit=bool(final.get("cache_hit")),
            blocked=final.get("outcome") == "blocked",
        )
        session.add(msg)
        await session.flush()
        session.add(
            Trace(
                id=trace_id,
                conversation_id=prepared.conversation_id,
                message_id=msg.id,
                user_id=prepared.user_id,
                steps=json.loads(json.dumps(final.get("trace", []), default=str)),
                total_ms=latency_ms,
            )
        )
        session.add_all(llm_call_rows(collector.calls, trace_id))
        conversation = await session.get(Conversation, prepared.conversation_id)
        if conversation is not None:
            conversation.updated_at = utcnow()
        await session.commit()
        return msg.id


async def _enqueue_followups(
    prepared: PreparedTurn,
    final: dict[str, Any],
    user: User,
    assistant_id: uuid.UUID,
    memory: MemoryContext,
) -> None:
    """Background work, each job only when it can do something (they cost model calls)."""
    q = get_enqueuer()
    cid = str(prepared.conversation_id)
    queued: list[str] = []
    if prepared.first_turn:
        await q.enqueue("title_conversation", cid, job_id=f"title:{cid}")
        queued.append("title_conversation")
    if memory.needs_summary_after(prepared.message, final.get("answer", "")):
        await q.enqueue("summarize_conversation", cid, job_id=f"summarize:{cid}:{assistant_id}")
        queued.append("summarize_conversation")
    outcome = final.get("outcome")
    if (
        user.memory_enabled
        and outcome in ("answered", "smalltalk")
        and long_term.worth_remembering(prepared.message)
    ):
        await q.enqueue(
            "extract_memories",
            str(user.id),
            str(prepared.user_message_id),
            str(assistant_id),
            job_id=f"memory:{assistant_id}",
        )
        queued.append("extract_memories")
    if outcome == "answered" and random.random() < settings.online_eval_sample_rate:
        await q.enqueue("online_eval", str(assistant_id), "sample", job_id=f"oe:{assistant_id}")
        queued.append("online_eval")
    obs.event("followups.enqueued", output=queued)


def _score_turn(trace_id: str, final: dict[str, Any], payload: dict[str, Any]) -> None:
    """Turn-level Langfuse scores: filterable outcome, cache and verifier signals."""
    obs.score(trace_id, "outcome", payload["outcome"], data_type="CATEGORICAL")
    if payload.get("fallback"):
        obs.score(trace_id, "fallback", payload["fallback"], data_type="CATEGORICAL")
    obs.score(trace_id, "cache_hit", bool(payload.get("cache_hit")))
    verification = final.get("verification") or {}
    if verification and not verification.get("skipped"):
        for key in ("grounded", "complete"):
            if key in verification:
                obs.score(trace_id, key, bool(verification[key]), comment=verification.get("reason") or None)
    obs.score(trace_id, "replans", float(final.get("replans", 0)), data_type="NUMERIC")
    obs.score(trace_id, "revisions", float(final.get("revisions", 0)), data_type="NUMERIC")


async def execute_turn(prepared: PreparedTurn, sink: EventSink = _noop) -> dict[str, Any]:
    trace_id = uuid.uuid4()
    lf_trace_id = obs.trace_id_for(trace_id)
    with (
        obs.propagate(
            user_id=str(prepared.user_id),
            session_id=str(prepared.conversation_id),
            trace_name="chat-turn",
            tags=["chat", prepared.role, settings.env],
            version=f"{settings.app_version}+prompts.{PROMPT_VERSION}",
            metadata={"turn_id": prepared.turn_id, "first_turn": prepared.first_turn},
        ),
        obs.observe(
            "chat-turn",
            as_type="agent",
            trace_id=lf_trace_id,
            input={"message": prepared.message},
            metadata={
                "turn_id": prepared.turn_id,
                "user_message_id": str(prepared.user_message_id),
                "first_turn": prepared.first_turn,
                "models": {
                    "fast": settings.model_fast,
                    "code": settings.model_code,
                    "synth": settings.model_synth,
                },
            },
        ) as root,
    ):
        final, payload = await _execute_turn(prepared, sink, trace_id)
        root.update(
            output=payload["answer"],
            metadata={
                "outcome": payload["outcome"],
                "fallback": payload["fallback"],
                "cache_hit": payload["cache_hit"],
                "standalone_query": payload["standalone_query"],
                "latency_ms": payload["latency_ms"],
                "usage": payload["usage"],
                "message_id": payload["message_id"],
                "plan": [f"{s['id']}:{s['tool']}" for s in payload["plan"]],
                "citations": [c.get("tag") for c in final.get("citations", [])],
            },
        )
        if payload["outcome"] == "error":
            root.fail(f"fallback: {payload['fallback']}")
        _score_turn(lf_trace_id, final, payload)
    return payload


async def _execute_turn(
    prepared: PreparedTurn, sink: EventSink, trace_id: uuid.UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    structlog.contextvars.bind_contextvars(
        conversation_id=str(prepared.conversation_id), trace_id=str(trace_id), user_id=str(prepared.user_id)
    )
    await sink(
        {
            "type": "meta",
            "turn_id": prepared.turn_id,
            "conversation_id": str(prepared.conversation_id),
            "user_message_id": str(prepared.user_message_id),
            "trace_id": str(trace_id),
        }
    )
    collector, token = start_collection()
    ctx: UserContext | None = None
    user: User | None = None
    memory = MemoryContext()
    state: dict[str, Any] = {}
    try:
        with obs.observe("load-context") as ctx_obs:
            async with SessionLocal() as session:
                user = await session.get(User, prepared.user_id)
                conversation = await session.get(Conversation, prepared.conversation_id)
                if user is None or conversation is None:
                    raise AppError("Conversation no longer exists", code="conversation_gone")
                ctx = await load_user_context(session, user)
                memory = await build_context(
                    session,
                    conversation=conversation,
                    user=user,
                    message=prepared.message,
                    exclude_message_id=prepared.user_message_id,
                )
            ctx_obs.update(
                output={
                    "profile": ctx.profile_text(),
                    "recent_turns": memory.recent_turns,
                    "summary": memory.summary,
                    "memories": memory.memories,
                },
                metadata={
                    "history_turns": len(memory.lines),
                    "has_summary": bool(memory.summary),
                    "memory_enabled": user.memory_enabled,
                    "has_member_profile": bool(ctx.member_id),
                },
            )
        if user.role != "admin":
            sink = _member_sink(sink)
        state = dict(
            initial_state(
                turn_id=prepared.turn_id,
                conversation_id=str(prepared.conversation_id),
                user=ctx.as_dict(),
                message=prepared.message,
                recent_turns=memory.recent_turns,
                summary=memory.summary,
                memories=memory.memories,
                data_version=await get_data_version(),
            )
        )
        async with asyncio.timeout(settings.turn_timeout_seconds):
            final = await run_graph(
                get_graph(),
                state,  # type: ignore[arg-type]
                thread_id=str(prepared.conversation_id),
                sink=sink,
            )
    except TimeoutError:
        log.warning("turn_timed_out", timeout_s=settings.turn_timeout_seconds)
        obs.current().fail(f"turn timed out after {settings.turn_timeout_seconds:.0f}s")
        final = {**state, "outcome": "error", "fallback": "timeout", "answer": FALLBACKS["timeout"]}
        await sink(
            {"type": "error", "code": "turn_timeout", "message": "The turn took too long", "retryable": True}
        )
        await sink({"type": "answer_replaced", "answer": final["answer"], "reason": "timeout"})
    except Exception as exc:
        log.exception("turn_failed")
        obs.current().fail(exc)
        reason = "llm_unavailable"
        final = {**state, "outcome": "error", "fallback": reason, "answer": FALLBACKS[reason]}
        await sink({"type": "error", "code": "turn_failed", "message": str(exc)[:200], "retryable": True})
    finally:
        stop_collection(token)

    latency_ms = int((time.monotonic() - prepared.started) * 1000)
    with obs.observe("persist", metadata={"llm_calls": len(collector.calls)}) as persist_obs:
        assistant_id = await _persist(prepared, final, collector, trace_id=trace_id, latency_ms=latency_ms)
        persist_obs.update(output={"message_id": str(assistant_id)})
    # Admins get the full trace and generated code; members get the plan, steps and verdict
    # (everything is persisted either way and readable by admins via /admin/traces).
    admin = user is not None and user.role == "admin"
    tools = _tool_summaries(final.get("results", []))
    citations = final.get("citations", [])
    payload = {
        "message_id": str(assistant_id),
        "conversation_id": str(prepared.conversation_id),
        "answer": final.get("answer", ""),
        "outcome": final.get("outcome") or "error",
        "fallback": final.get("fallback") or None,
        "cache_hit": bool(final.get("cache_hit")),
        "standalone_query": final.get("standalone_query", ""),
        "plan": final.get("plan", []),
        "tools": tools if admin else member_tools(tools),
        "citations": citations if admin else member_citations(citations),
        "verification": final.get("verification") or None,
        "latency_ms": latency_ms,
        "usage": collector.totals,
        "trace_id": str(trace_id),
        "trace": final.get("trace", []) if admin else [],
        "langfuse_url": await asyncio.to_thread(obs.trace_url, trace_id) if admin and obs.enabled() else None,
    }
    await sink({"type": "final", **payload})
    TURN_LATENCY.observe(latency_ms / 1000)

    if ctx is not None and user is not None:
        try:
            with obs.observe("semantic_cache.store", input=final.get("cache_query") or None) as store_obs:
                scope = await maybe_store(final, ctx, source_message_id=str(assistant_id))
                store_obs.update(output={"stored": scope is not None, "scope": scope})
            await _enqueue_followups(prepared, final, user, assistant_id, memory)
        except Exception as exc:  # follow-ups never fail the turn
            log.warning("turn_followups_failed", error=str(exc)[:200])
    structlog.contextvars.unbind_contextvars("conversation_id", "trace_id")
    return final, payload


async def run_turn(prepared: PreparedTurn, sink: EventSink = _noop) -> dict[str, Any]:
    """Execute a turn and always release the conversation lock."""
    try:
        return await execute_turn(prepared, sink)
    finally:
        await release(prepared)


def _sse(event: dict[str, Any]) -> dict[str, str]:
    data = {k: v for k, v in event.items() if k != "type"}
    return {"event": str(event["type"]), "data": json.dumps(data, default=str)}


def stream_turn(prepared: PreparedTurn) -> EventSourceResponse:
    """SSE response fed by a background task; the task finishes (and persists) even if the client leaves."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def sink(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def worker() -> None:
        try:
            await run_turn(prepared, sink)
        except Exception as exc:
            log.exception("turn_worker_failed")
            await queue.put(
                {"type": "error", "code": "internal_error", "message": str(exc)[:200], "retryable": True}
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(worker())
    _background.add(task)
    task.add_done_callback(_background.discard)

    async def events() -> AsyncIterator[dict[str, str]]:
        while (event := await queue.get()) is not None:
            yield _sse(event)

    return EventSourceResponse(
        events(), ping=15, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    )


async def drain_background(grace_seconds: float = 30.0) -> None:
    """Wait for in-flight turns (graceful shutdown)."""
    if _background:
        await asyncio.wait(list(_background), timeout=grace_seconds)
