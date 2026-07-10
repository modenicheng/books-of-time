from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_turning_points_cli_parser_and_empty_export(tmp_path) -> None:
    args = cli.build_parser().parse_args(
        [
            "event",
            "turning-points",
            "event-a",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--bucket-minutes",
            "30",
            "--output",
            "turning.jsonl",
        ]
    )
    assert args.event_command == "turning-points"
    assert args.bucket_minutes == 30

    url = f"sqlite+aiosqlite:///{tmp_path / 'turning.sqlite3'}"
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

    output = tmp_path / "turning.jsonl"
    count = await cli._export_turning_points(
        {"database": {"url": url}},
        event_reference="event-a",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        bucket_minutes=30,
        spike_multiplier=3.0,
        min_count=5,
        turnover_threshold=0.5,
        top_n=20,
        output_path=output,
    )
    assert count == 0
    assert output.read_text(encoding="utf-8") == ""
