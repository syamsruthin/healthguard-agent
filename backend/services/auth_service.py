"""
Authentication: registration, login, refresh-token rotation with reuse
detection, logout.

Refresh tokens are opaque random strings; only their SHA-256 is stored. Each
login starts a token *family*; every refresh revokes the presented token and
issues its successor in the same family. Presenting an already-revoked token
means it was stolen or replayed, so the whole family is revoked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.errors import AppError, AuthError, ConflictError
from backend.core.logging import get_logger
from backend.core.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)
from backend.db.base import utcnow
from backend.db.models import RefreshToken, User

log = get_logger(__name__)

# Verified against on unknown emails so response time does not reveal which accounts exist.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret


async def register(session: AsyncSession, *, email: str, password: str, full_name: str) -> User:
    email = email.strip().lower()
    if len(password) < settings.password_min_length:
        raise AppError(
            f"Password must be at least {settings.password_min_length} characters", code="weak_password"
        )
    if (await session.execute(select(User.id).where(User.email == email))).first():
        raise ConflictError("An account with this email already exists", code="email_taken")
    # Self-registered accounts are never linked to member records (that needs identity verification).
    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        role="member",
        member_id=None,
    )
    session.add(user)
    await session.commit()
    log.info("user_registered", user_id=str(user.id))
    return user


async def _issue(
    session: AsyncSession, user: User, family_id: uuid.UUID | None = None
) -> tuple[TokenPair, RefreshToken]:
    access, ttl = create_access_token(
        user_id=user.id, role=user.role, member_id=user.member_id, email=user.email
    )
    raw, digest = new_refresh_token()
    token = RefreshToken(
        user_id=user.id,
        token_hash=digest,
        family_id=family_id or uuid.uuid4(),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
    )
    session.add(token)
    await session.flush()
    return TokenPair(access_token=access, refresh_token=raw, expires_in=ttl), token


async def login(session: AsyncSession, *, email: str, password: str) -> tuple[User, TokenPair]:
    user = (
        await session.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise AuthError("Invalid email or password", code="invalid_credentials")
    if not verify_password(password, user.password_hash) or not user.is_active:
        raise AuthError("Invalid email or password", code="invalid_credentials")
    user.last_login_at = utcnow()
    pair, _ = await _issue(session, user)
    await session.commit()
    log.info("user_login", user_id=str(user.id))
    return user, pair


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def refresh(session: AsyncSession, raw_token: str) -> tuple[User, TokenPair]:
    token = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)).with_for_update()
        )
    ).scalar_one_or_none()
    if token is None:
        raise AuthError("Invalid refresh token", code="invalid_refresh_token")
    if token.revoked_at is not None:
        await _revoke_family(session, token.family_id)
        await session.commit()
        log.warning("refresh_token_reuse", user_id=str(token.user_id), family=str(token.family_id))
        raise AuthError("Refresh token was already used; please sign in again", code="refresh_token_reused")
    if token.expires_at <= utcnow():
        raise AuthError("Refresh token expired; please sign in again", code="refresh_token_expired")
    user = await session.get(User, token.user_id)
    if user is None or not user.is_active:
        raise AuthError("Account is disabled", code="account_disabled")
    pair, successor = await _issue(session, user, family_id=token.family_id)
    token.revoked_at = utcnow()
    token.replaced_by = successor.id
    await session.commit()
    return user, pair


async def logout(session: AsyncSession, raw_token: str) -> None:
    token = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))
    ).scalar_one_or_none()
    if token is not None:
        await _revoke_family(session, token.family_id)
        await session.commit()
