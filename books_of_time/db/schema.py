from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine

from books_of_time.config import load_config
from books_of_time.db import models as _models  # noqa: F401
from books_of_time.db.base import Base


async def create_schema(config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
