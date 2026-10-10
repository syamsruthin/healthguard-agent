"""Application tables: identity, chats, feedback, memory, traces, usage, evals."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.config import settings
from backend.db.base import APP_SCHEMA, Base, created_at_col, uuid_pk

_S = {"schema": APP_SCHEMA}


class User(Base):
    __tablename__ = "users"
    __table_args__ = _S

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="member")  # member | admin
    # Link to the synthetic member record (drives Row-Level Security for member data).
    member_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = _S

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"), _S)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{APP_SCHEMA}.users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    # Rolling summary of turns that have fallen out of the verbatim window.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_upto: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_eval: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.created_at", lazy="raise"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"), _S)

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.conversations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{APP_SCHEMA}.users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    standalone_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    tools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()

    conversation: Mapped[Conversation] = relationship(back_populates="messages", lazy="raise")


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_feedback_message_user"), _S)

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.messages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{APP_SCHEMA}.users.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(SmallInteger)  # +1 / -1
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class UserMemory(Base):
    """Durable, non-clinical user preferences extracted from conversations."""

    __tablename__ = "user_memories"
    __table_args__ = _S

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="preference")
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class Trace(Base):
    __tablename__ = "traces"
    __table_args__ = _S

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True, nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created", "created_at"), _S)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True, nullable=True)
    role: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    exact_cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = _S

    id: Mapped[uuid.UUID] = uuid_pk()
    suite: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|completed|failed
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvalResult(Base):
    __tablename__ = "eval_results"
    __table_args__ = _S

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.eval_runs.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(64))
    persona: Mapped[str | None] = mapped_column(String(64), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    contexts: Mapped[list[str]] = mapped_column(JSONB, default=list)
    expected_tools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    actual_tools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class OnlineEvalScore(Base):
    __tablename__ = "online_eval_scores"
    __table_args__ = (Index("ix_online_eval_created", "created_at"), _S)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.messages.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(64))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = created_at_col()
