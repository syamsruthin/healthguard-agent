"""
Rolling conversation summary.

Turns that no longer fit the verbatim history window are folded into
`conversations.summary`; `summary_upto` marks the last message covered, so the
context builder only reads later messages and the two never overlap.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.tokens import count_tokens
from backend.db.models import Conversation
from backend.llm.client import get_llm
from backend.llm.prompts.memory import build_summary_messages
from backend.llm.registry import Role
from backend.memory.context import format_turn, history_messages, split_window

log = get_logger(__name__)


async def maybe_summarize(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Summarize turns outside the history window once they exceed SUMMARY_TRIGGER_TOKENS."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        return False
    msgs = await history_messages(session, conversation)
    lines = [format_turn(m.role, m.content) for m in msgs]
    older, _ = split_window(lines, settings.history_token_budget)
    if not older or count_tokens("\n".join(older)) < settings.summary_trigger_tokens:
        return False
    result = await get_llm().complete(
        Role.FAST,
        build_summary_messages(previous_summary=conversation.summary or "", transcript="\n".join(older)),
        name="summary",
    )
    conversation.summary = result.text.strip()
    conversation.summary_upto = msgs[len(older) - 1].created_at
    await session.commit()
    log.info("conversation_summarized", conversation_id=str(conversation_id), turns=len(older))
    return True
