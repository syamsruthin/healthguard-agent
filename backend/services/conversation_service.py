"""Conversation CRUD, scoped to the owning user (other users get 404, never 403)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.graph import get_graph
from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.db.base import utcnow
from backend.db.models import Conversation, Message, User

log = get_logger(__name__)


async def create(
    session: AsyncSession, user: User, *, title: str | None = None, is_eval: bool = False
) -> Conversation:
    conv = Conversation(user_id=user.id, title=(title or "New conversation")[:200], is_eval=is_eval)
    session.add(conv)
    await session.commit()
    return conv


async def list_for_user(
    session: AsyncSession, user: User, *, limit: int = 50, offset: int = 0
) -> list[Conversation]:
    rows = await session.execute(
        select(Conversation)
        .where(
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
            Conversation.is_eval.is_(False),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars())


async def get_owned(session: AsyncSession, user: User, conversation_id: uuid.UUID) -> Conversation:
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id or conv.deleted_at is not None:
        raise NotFoundError("Conversation not found")
    return conv


async def rename(session: AsyncSession, user: User, conversation_id: uuid.UUID, title: str) -> Conversation:
    conv = await get_owned(session, user, conversation_id)
    conv.title = title.strip()[:200] or conv.title
    conv.updated_at = utcnow()
    await session.commit()
    return conv


async def delete(session: AsyncSession, user: User, conversation_id: uuid.UUID) -> None:
    """Soft-delete the conversation and drop its agent checkpoints."""
    conv = await get_owned(session, user, conversation_id)
    conv.deleted_at = utcnow()
    await session.commit()
    checkpointer = get_graph().checkpointer
    if checkpointer is not None and not isinstance(checkpointer, bool):
        try:
            await checkpointer.adelete_thread(str(conversation_id))
        except Exception as exc:
            log.warning(
                "checkpoint_delete_failed", conversation_id=str(conversation_id), error=str(exc)[:200]
            )


async def messages(session: AsyncSession, conversation: Conversation, *, limit: int = 200) -> list[Message]:
    rows = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.role.desc())  # user before assistant on equal timestamps
        .limit(limit)
    )
    return list(rows.scalars())


async def message_count(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
            )
        ).scalar_one()
    )
