from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_propagation_replay_cli_parser_and_empty_export(tmp_path) -> None:
    args = cli.build_parser().parse_args(
        [
            "event",
            "replay-propagation",
            "event-a",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T01:00:00Z",
            "--output",
            "chain.jsonl",
        ]
    )
    assert args.event_command == "replay-propagation"

    url = f"sqlite+aiosqlite:///{tmp_path / 'chain.sqlite3'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await EventRepository(session).create_event(
            slug="event-a",
            name="A",
            now=datetime(2026, 7, 10, tzinfo=UTC),
        )
        await session.commit()
    await engine.dispose()
    output = tmp_path / "chain.jsonl"
    count = await cli._export_propagation_replay(
        {"database": {"url": url}},
        event_reference="event-a",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T01:00:00Z",
        max_records=10000,
        output_path=output,
    )
    assert count == 0
    assert output.read_text(encoding="utf-8") == ""
