"""
arq background tasks. Each runs inside its own usage collection, and its LLM
calls are persisted to `app.llm_calls` (without a trace id) so background
cost shows up in the admin dashboards.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.db.models import Conversation, LLMCall, Message, User
from backend.db.session import SessionLocal
from backend.evals.online import score_message
from backend.llm.client import get_llm
from backend.llm.prompts.memory import build_title_messages
from backend.llm.prompts.schemas import Title
from backend.llm.registry import Role
from backend.llm.usage import UsageCollector, start_collection, stop_collection
from backend.memory import long_term
from backend.memory.summarizer import maybe_summarize

log = get_logger(__name__)
DEFAULT_TITLE = "New conversation"


async def _persist_calls(collector: UsageCollector) -> None:
    if not collector.calls:
        return
    async with SessionLocal() as session:
        session.add_all(
            LLMCall(
                trace_id=None,
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
        await session.commit()


async def _accounted[T](fn: Callable[[], Awaitable[T]]) -> T:
    collector, token = start_collection()
    try:
        return await fn()
    finally:
        stop_collection(token)
        await _persist_calls(collector)


async def title_conversation(ctx: dict[str, Any], conversation_id: str) -> str | None:
    async def run() -> str | None:
        async with SessionLocal() as session:
            conv = await session.get(Conversation, uuid.UUID(conversation_id))
            if conv is None or conv.title != DEFAULT_TITLE:
                return None
            first = (
                await session.execute(
                    select(Message.content)
                    .where(Message.conversation_id == conv.id, Message.role == "user")
                    .order_by(Message.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not first:
                return None
            title = await get_llm().structured(Role.FAST, build_title_messages(first), Title, name="title")
            conv.title = title.title.strip().strip('"').rstrip(".")[:200] or DEFAULT_TITLE
            await session.commit()
            return conv.title

    return await _accounted(run)


async def summarize_conversation(ctx: dict[str, Any], conversation_id: str) -> bool:
    lock = f"hg:lock:summarize:{conversation_id}"
    if not await get_redis().set(lock, "1", nx=True, ex=120):
        return False  # another worker is already summarizing this conversation
    try:

        async def run() -> bool:
            async with SessionLocal() as session:
                return await maybe_summarize(session, uuid.UUID(conversation_id))

        return await _accounted(run)
    finally:
        await get_redis().delete(lock)


async def extract_memories(
    ctx: dict[str, Any], user_id: str, user_message_id: str, assistant_message_id: str
) -> int:
    async def run() -> int:
        async with SessionLocal() as session:
            user = await session.get(User, uuid.UUID(user_id))
            user_msg = await session.get(Message, uuid.UUID(user_message_id))
            assistant_msg = await session.get(Message, uuid.UUID(assistant_message_id))
            if user is None or user_msg is None or assistant_msg is None or not user.memory_enabled:
                return 0
            stored = await long_term.extract_and_store(
                session,
                user,
                user_message=user_msg.content,
                assistant_message=assistant_msg.content,
                source_message_id=assistant_msg.id,
            )
            return len(stored)

    return await _accounted(run)


async def run_offline_eval(ctx: dict[str, Any], run_id: str, limit: int | None = None) -> dict[str, Any]:
    """Golden-suite evaluation started from the admin UI (judge calls are not usage-accounted)."""
    from backend.evals.run import execute  # heavy import (RAGAS) only when an eval actually runs

    outcome = await execute(run_id=uuid.UUID(run_id), limit=limit)
    return {"run_id": outcome["run_id"], "passed": outcome["passed"], "report": outcome["report"]}


async def online_eval(
    ctx: dict[str, Any], message_id: str, reason: str = "sample"
) -> dict[str, float] | None:
    async def run() -> dict[str, float] | None:
        async with SessionLocal() as session:
            scores = await score_message(session, uuid.UUID(message_id))
        log.info("online_eval_scored", message_id=message_id, reason=reason, scores=scores)
        return scores

    return await _accounted(run)
