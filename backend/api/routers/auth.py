from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import auth_rate_limit, get_current_user
from backend.api.schemas import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut
from backend.db.models import User
from backend.db.session import get_session
from backend.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(user: User, pair: auth_service.TokenPair) -> TokenOut:
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        user=UserOut.model_validate(user),
    )


@router.post(
    "/register", status_code=status.HTTP_201_CREATED, dependencies=[Depends(auth_rate_limit("register"))]
)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    await auth_service.register(session, email=body.email, password=body.password, full_name=body.full_name)
    user, pair = await auth_service.login(session, email=body.email, password=body.password)
    return _tokens(user, pair)


@router.post("/login", dependencies=[Depends(auth_rate_limit("login"))])
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user, pair = await auth_service.login(session, email=body.email, password=body.password)
    return _tokens(user, pair)


@router.post("/refresh", dependencies=[Depends(auth_rate_limit("refresh"))])
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user, pair = await auth_service.refresh(session, body.refresh_token)
    return _tokens(user, pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshIn, session: AsyncSession = Depends(get_session)) -> None:
    await auth_service.logout(session, body.refresh_token)


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)
