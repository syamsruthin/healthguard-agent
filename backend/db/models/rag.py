"""Retrieval tables (schema `rag`): documents, hybrid-indexed chunks, data version."""

from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, Date, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.config import settings
from backend.db.base import RAG_SCHEMA, Base

_S = {"schema": RAG_SCHEMA}


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = _S

    doc_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(40), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    source_path: Mapped[str] = mapped_column(String(300))
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        _S,
    )

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey(f"{RAG_SCHEMA}.documents.doc_id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    section_path: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(section_path, '') || ' ' || content)", persisted=True),
    )


class DataVersion(Base):
    """Monotonic version bumped on every seed/ingest — part of every cache key."""

    __tablename__ = "data_version"
    __table_args__ = _S

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
