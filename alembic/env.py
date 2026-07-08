"""Alembic env -- async + YAML config.

DB url is read from ``config/config.yaml`` instead of ``alembic.ini``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ---------------------------------------------------------------------------
# Alembic Config + logging
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# read DB URL from YAML (highest priority)
# ---------------------------------------------------------------------------
_here = Path(__file__).resolve().parent.parent
_config_yaml = _here / "config" / "config.yaml"
_yaml_provided = False

if _config_yaml.exists():
    import yaml

    with open(_config_yaml, encoding="utf-8") as f:
        yaml_cfg = yaml.safe_load(f)
    db_url = yaml_cfg.get("database", {}).get("url", "")
    if db_url and "placeholder" not in db_url:
        _yaml_provided = True
        config.set_main_option("sqlalchemy.url", db_url)

if not _yaml_provided:
    # fallback to ini value (for offline mode only)
    db_url = config.get_main_option("sqlalchemy.url", "")

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
    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
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
