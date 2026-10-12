from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user
from backend.api.schemas import MemoryOut, SettingsPatch, UserOut
from backend.core.errors import NotFoundError
from backend.db.models import User
from backend.db.session import get_session
from backend.memory import long_term
from backend.services import coverage_service
from backend.services.user_context import load_user_context

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/coverage")
async def coverage(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    ctx = await load_user_context(session, user)
    return await coverage_service.member_card(ctx)


@router.get("/memories")
async def list_memories(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> list[MemoryOut]:
    return [MemoryOut.model_validate(m) for m in await long_term.list_memories(session, user.id)]


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> None:
    if not await long_term.delete_memory(session, user.id, memory_id):
        raise NotFoundError("Memory not found")


@router.delete("/memories", status_code=status.HTTP_204_NO_CONTENT)
async def clear_memories(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> None:
    await long_term.clear_memories(session, user.id)


@router.patch("/settings")
async def update_settings(
    body: SettingsPatch, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> UserOut:
    user.memory_enabled = body.memory_enabled
    await session.commit()
    return UserOut.model_validate(user)
