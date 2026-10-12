"""Request / response models for the public API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=512)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    member_id: str | None
    memory_enabled: bool


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    user: UserOut


class ConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    citations: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    trace_id: uuid.UUID | None = None
    latency_ms: int | None = None
    cache_hit: bool = False
    blocked: bool = False
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    meta: dict[str, Any] = {}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class FeedbackIn(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=2000)


class SettingsPatch(BaseModel):
    memory_enabled: bool


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    category: str
    created_at: datetime
