"""arq worker entry point: `arq backend.workers.settings.WorkerSettings`."""

from __future__ import annotations

from typing import Any, ClassVar

from arq import func
from arq.connections import RedisSettings

from backend.agents.tools import sandbox
from backend.core.config import settings
from backend.core.logging import configure_logging, get_logger
from backend.core.redis import close_redis
from backend.db.session import dispose
from backend.workers import tasks

log = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    log.info("worker_started")


async def shutdown(ctx: dict[str, Any]) -> None:
    sandbox.shutdown()
    await close_redis()
    await dispose()
    log.info("worker_stopped")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        tasks.title_conversation,
        tasks.summarize_conversation,
        tasks.extract_memories,
        tasks.online_eval,
        func(
            tasks.run_offline_eval, timeout=3 * 3600
        ),  # a full golden run takes far longer than a chat follow-up
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 300
    keep_result = 3600
