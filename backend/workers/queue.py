"""
Background-job enqueueing seam.

The API enqueues follow-up work (titles, summaries, memory extraction,
online evaluation) through this interface, so request handling never depends
on the worker being up and tests can record jobs instead of running them.
"""

from __future__ import annotations

from typing import Any, Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from backend.core.config import settings
from backend.core.logging import get_logger

log = get_logger(__name__)


class Enqueuer(Protocol):
    async def enqueue(self, function: str, *args: Any, job_id: str | None = None) -> None: ...

    async def close(self) -> None: ...


class ArqEnqueuer:
    def __init__(self, pool: ArqRedis):
        self._pool = pool

    @classmethod
    async def create(cls) -> ArqEnqueuer:
        return cls(await create_pool(RedisSettings.from_dsn(settings.redis_url)))

    async def enqueue(self, function: str, *args: Any, job_id: str | None = None) -> None:
        try:
            await self._pool.enqueue_job(function, *args, _job_id=job_id)
        except Exception as exc:  # follow-up work is best effort
            log.warning("enqueue_failed", function=function, error=str(exc)[:200])

    async def close(self) -> None:
        await self._pool.aclose()


class NullEnqueuer:
    async def enqueue(self, function: str, *args: Any, job_id: str | None = None) -> None:
        return None

    async def close(self) -> None:
        return None


class RecordingEnqueuer:
    """Test double: remembers every job."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], str | None]] = []

    async def enqueue(self, function: str, *args: Any, job_id: str | None = None) -> None:
        self.jobs.append((function, args, job_id))

    async def close(self) -> None:
        return None

    def names(self) -> list[str]:
        return [j[0] for j in self.jobs]


_enqueuer: Enqueuer = NullEnqueuer()


def get_enqueuer() -> Enqueuer:
    return _enqueuer


def set_enqueuer(enqueuer: Enqueuer | None) -> None:
    global _enqueuer
    _enqueuer = enqueuer or NullEnqueuer()
