from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse

from backend.api.deps import chat_rate_limit, get_current_user
from backend.api.schemas import ChatIn, ConversationIn, ConversationOut, ConversationPatch, MessageOut
from backend.db.models import User
from backend.db.session import get_session
from backend.services import chat_service, conversation_service

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(
    body: ConversationIn, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> ConversationOut:
    return ConversationOut.model_validate(await conversation_service.create(session, user, title=body.title))


@router.get("")
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationOut]:
    rows = await conversation_service.list_for_user(session, user, limit=limit, offset=offset)
    return [ConversationOut.model_validate(c) for c in rows]


@router.patch("/{conversation_id}")
async def rename(
    conversation_id: uuid.UUID,
    body: ConversationPatch,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    return ConversationOut.model_validate(
        await conversation_service.rename(session, user, conversation_id, body.title)
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await conversation_service.delete(session, user, conversation_id)


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    conv = await conversation_service.get_owned(session, user, conversation_id)
    out = [MessageOut.model_validate(m) for m in await conversation_service.messages(session, conv)]
    if user.role != "admin":  # generated SQL / pandas code is an internal detail
        for m in out:
            m.tools = chat_service.member_tools(m.tools)
            m.citations = chat_service.member_citations(m.citations)
    return out


@router.post("/{conversation_id}/messages", dependencies=[Depends(chat_rate_limit)])
async def send_message(
    conversation_id: uuid.UUID,
    body: ChatIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Non-streaming turn: waits for the full answer."""
    prepared = await chat_service.prepare_turn(session, user, conversation_id, body.message)
    return await chat_service.run_turn(prepared)


@router.post("/{conversation_id}/messages/stream", dependencies=[Depends(chat_rate_limit)])
async def stream_message(
    conversation_id: uuid.UUID,
    body: ChatIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    """Streaming turn (Server-Sent Events): meta, status, plan, tool, token, answer_reset,
    answer_replaced, citations, error, final."""
    prepared = await chat_service.prepare_turn(session, user, conversation_id, body.message)
    return chat_service.stream_turn(prepared)
