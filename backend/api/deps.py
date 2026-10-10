"""FastAPI dependencies: authentication, authorisation, rate limits."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import jwt
import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.errors import AuthError, ForbiddenError
from backend.core.security import decode_access_token
from backend.db.models import User
from backend.db.session import get_session
from backend.services import rate_limit

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise AuthError("Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_access_token(creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise AuthError(
            "Invalid or expired token", code="invalid_token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("Account not found or disabled", code="invalid_token")
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise ForbiddenError("Admin access required")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def auth_rate_limit(bucket: str) -> Callable[[Request], Awaitable[None]]:
    async def dep(request: Request) -> None:
        await rate_limit.hit(f"auth:{bucket}:{client_ip(request)}", settings.rate_limit_auth_per_minute)

    return dep


async def chat_rate_limit(user: User = Depends(get_current_user)) -> None:
    await rate_limit.hit(f"chat:{user.id}", settings.rate_limit_chat_per_minute)
