"""Alembic environment — async engine, multi-schema autogenerate."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.core.config import settings
from backend.db import models  # noqa: F401  (populate metadata)
from backend.db.base import APP_SCHEMA, INSURANCE_SCHEMA, RAG_SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata
MANAGED_SCHEMAS = {APP_SCHEMA, INSURANCE_SCHEMA, RAG_SCHEMA}


def include_name(name, type_, parent_names):  # type: ignore[no-untyped-def]
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def _configure(connection: Connection | None = None, **kwargs) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="public",
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=settings.database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
