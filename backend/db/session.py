"""
Database engines and sessions.

* `engine` / `SessionLocal` — the application role (full access to app/rag/insurance).
* `get_agent_pool()` — a separate asyncpg pool logged in as the read-only
  `hg_agent_ro` role. It is the ONLY connection LLM-generated SQL ever runs
  on, and every query executes inside a READ ONLY transaction with the
  caller's member id / role set for Row-Level Security.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.config import settings
from backend.core.telemetry import instrument_sqlalchemy

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)
instrument_sqlalchemy(engine)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

_agent_pool: asyncpg.Pool | None = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed by services explicitly."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for workers/scripts: commit on success, rollback on error."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_agent_pool() -> asyncpg.Pool:
    global _agent_pool
    if _agent_pool is None:
        _agent_pool = await asyncpg.create_pool(
            settings.agent_database_dsn, min_size=1, max_size=settings.db_pool_size, command_timeout=30
        )
    return _agent_pool


async def get_data_version() -> int:
    async with SessionLocal() as session:
        result = await session.execute(text("SELECT version FROM rag.data_version WHERE id = 1"))
        value = result.scalar_one_or_none()
        return int(value or 0)


async def dispose() -> None:
    global _agent_pool
    if _agent_pool is not None:
        await _agent_pool.close()
        _agent_pool = None
    await engine.dispose()
