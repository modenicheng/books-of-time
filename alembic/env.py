"""Alembic environment using the application configuration loader."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from books_of_time.config import load_config

# ---------------------------------------------------------------------------
# Alembic Config + logging
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None and not config.attributes.get(
    "skip_logger_config", False
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

app_config = load_config(config.attributes.get("bot_config_path"))
db_url = str(app_config["database"]["url"])
config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

# ---------------------------------------------------------------------------
# target metadata -- autogenerate depends on this
# When you create ORM models, import them here so Alembic can detect changes:
#
#     from books_of_time.db import Base
#     from books_of_time.db import models
#
#     target_metadata = Base.metadata
# ---------------------------------------------------------------------------
from books_of_time.db import Base  # noqa: E402
from books_of_time.db import models as _models  # noqa: E402,F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode: generate SQL script without connecting to DB."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Run migrations on an existing connection (called by async wrapper)."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Async online mode: create async engine and run migrations."""
    cfg = {
        "sqlalchemy.url": db_url,
        "sqlalchemy.echo": False,
    }
    schema = os.environ.get("BOT_DATABASE_SCHEMA")
    connect_args = {"server_settings": {"search_path": schema}} if schema else {}
    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Online mode entry point (sync wrapper calls async impl)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
