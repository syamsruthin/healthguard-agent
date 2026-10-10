"""
Conversation context for a turn.

* recent turns — verbatim messages after the rolling summary's cut-off,
  newest first until `HISTORY_TOKEN_BUDGET` is used (assistant replies are
  truncated; they are long and the user's wording matters more)
* the conversation's rolling summary of older turns
* long-term memories (added by the memory module)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.tokens import count_tokens
from backend.db.models import Conversation, Message, User

ASSISTANT_CHARS = 1_200


@dataclass
class MemoryContext:
    recent_turns: str = ""
    summary: str = ""
    memories: str = ""


def format_turn(role: str, content: str) -> str:
    if role == "assistant":
        text = content if len(content) <= ASSISTANT_CHARS else content[:ASSISTANT_CHARS] + " …"
        return f"Assistant: {text}"
    return f"User: {content}"


def split_window(lines: list[str], budget: int) -> tuple[list[str], list[str]]:
    """Split chronological turn lines into (older, window): the newest lines that fit `budget` tokens."""
    used = 0
    start = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        cost = count_tokens(lines[i])
        if used + cost > budget and start < len(lines):
            break
        used += cost
        start = i
    return lines[:start], lines[start:]


async def history_messages(
    session: AsyncSession, conversation: Conversation, *, exclude_message_id: uuid.UUID | None = None
) -> list[Message]:
    """Messages after the summary cut-off, chronological."""
    q = select(Message).where(Message.conversation_id == conversation.id)
    if conversation.summary_upto is not None:
        q = q.where(Message.created_at > conversation.summary_upto)
    if exclude_message_id is not None:
        q = q.where(Message.id != exclude_message_id)
    rows = await session.execute(q.order_by(Message.created_at, Message.role.desc()))
    return list(rows.scalars())


async def build_context(
    session: AsyncSession,
    *,
    conversation: Conversation,
    user: User,
    message: str,
    exclude_message_id: uuid.UUID | None = None,
) -> MemoryContext:
    msgs = await history_messages(session, conversation, exclude_message_id=exclude_message_id)
    lines = [format_turn(m.role, m.content) for m in msgs]
    _, window = split_window(lines, settings.history_token_budget)
    return MemoryContext(recent_turns="\n".join(window), summary=conversation.summary or "")
