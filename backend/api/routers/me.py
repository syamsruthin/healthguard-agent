from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user
from backend.db.models import User
from backend.db.session import get_session
from backend.services import coverage_service
from backend.services.user_context import load_user_context

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/coverage")
async def coverage(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    ctx = await load_user_context(session, user)
    return await coverage_service.member_card(ctx)
