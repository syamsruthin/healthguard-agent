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
from backend.core.config import settings
from backend.core.errors import AppError, ConflictError
from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.core.telemetry import TURN_LATENCY
from backend.db.base import utcnow
from backend.db.models import Conversation, LLMCall, Message, Trace, User
from backend.db.session import SessionLocal, get_data_version
from backend.llm.usage import UsageCollector, start_collection, stop_collection
from backend.memory import long_term
from backend.memory.context import MemoryContext, build_context
from backend.services import conversation_service
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
        session.add_all(
            LLMCall(
                trace_id=trace_id,
                role=c.role,
                name=c.name[:64],
                model=c.model[:128],
                prompt_tokens=c.prompt_tokens,
                completion_tokens=c.completion_tokens,
                cached_tokens=c.cached_tokens,
                cache_write_tokens=c.cache_write_tokens,
                cost_usd=Decimal(str(c.cost_usd)),
                latency_ms=c.latency_ms,
                exact_cache_hit=c.exact_cache_hit,
                error=c.error,
            )
            for c in collector.calls
        )
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
    if prepared.first_turn:
        await q.enqueue("title_conversation", cid, job_id=f"title:{cid}")
    if memory.needs_summary_after(prepared.message, final.get("answer", "")):
        await q.enqueue("summarize_conversation", cid, job_id=f"summarize:{cid}:{assistant_id}")
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
    if outcome == "answered" and random.random() < settings.online_eval_sample_rate:
        await q.enqueue("online_eval", str(assistant_id), "sample", job_id=f"oe:{assistant_id}")


async def execute_turn(prepared: PreparedTurn, sink: EventSink = _noop) -> dict[str, Any]:
    trace_id = uuid.uuid4()
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
        final = {**state, "outcome": "error", "fallback": "timeout", "answer": FALLBACKS["timeout"]}
        await sink(
            {"type": "error", "code": "turn_timeout", "message": "The turn took too long", "retryable": True}
        )
        await sink({"type": "answer_replaced", "answer": final["answer"], "reason": "timeout"})
    except Exception as exc:
        log.exception("turn_failed")
        reason = "llm_unavailable"
        final = {**state, "outcome": "error", "fallback": reason, "answer": FALLBACKS[reason]}
        await sink({"type": "error", "code": "turn_failed", "message": str(exc)[:200], "retryable": True})
    finally:
        stop_collection(token)

    latency_ms = int((time.monotonic() - prepared.started) * 1000)
    assistant_id = await _persist(prepared, final, collector, trace_id=trace_id, latency_ms=latency_ms)
    payload = {
        "message_id": str(assistant_id),
        "conversation_id": str(prepared.conversation_id),
        "answer": final.get("answer", ""),
        "outcome": final.get("outcome") or "error",
        "fallback": final.get("fallback") or None,
        "cache_hit": bool(final.get("cache_hit")),
        "standalone_query": final.get("standalone_query", ""),
        "tools": _tool_summaries(final.get("results", [])),
        "citations": final.get("citations", []),
        "verification": final.get("verification") or None,
        "latency_ms": latency_ms,
        "usage": collector.totals,
        "trace_id": str(trace_id),
        "trace": final.get("trace", []),
    }
    await sink({"type": "final", **payload})
    TURN_LATENCY.observe(latency_ms / 1000)

    if ctx is not None and user is not None:
        try:
            await maybe_store(final, ctx, source_message_id=str(assistant_id))
            await _enqueue_followups(prepared, final, user, assistant_id, memory)
        except Exception as exc:  # follow-ups never fail the turn
            log.warning("turn_followups_failed", error=str(exc)[:200])
    structlog.contextvars.unbind_contextvars("conversation_id", "trace_id")
    return payload


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
