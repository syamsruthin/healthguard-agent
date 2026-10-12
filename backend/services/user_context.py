"""Build the per-request `UserContext` (identity + member profile) for a user account."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.tools.base import UserContext
from backend.db.models import User


async def load_user_context(session: AsyncSession, user: User) -> UserContext:
    ctx = UserContext(
        user_id=str(user.id),
        role=user.role,
        email=user.email,
        full_name=user.full_name,
        member_id=user.member_id,
    )
    return await ctx.load_member_profile(session)


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (await session.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
