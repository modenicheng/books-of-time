from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


def get_expected_schema_revision(
    alembic_ini_path: str | Path | None = None,
) -> str:
    path = (
        Path(alembic_ini_path)
        if alembic_ini_path is not None
        else Path(__file__).resolve().parents[2] / "alembic.ini"
    )
    script = ScriptDirectory.from_config(Config(str(path)))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Expected one Alembic head, found: {heads}")
    return heads[0]


async def get_current_schema_revision(session: AsyncSession) -> str | None:
    connection = await session.connection()
    has_version_table = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).has_table("alembic_version")
    )
    if not has_version_table:
        return None
    revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    return str(revision) if revision is not None else None
