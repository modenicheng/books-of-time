from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base
from books_of_time.db.repositories import EventRepository


def test_template_candidates_parser_accepts_analysis_controls() -> None:
    args = cli.build_parser().parse_args(
        [
            "event",
            "template-candidates",
            "event-a",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--window-minutes",
            "30",
            "--min-similarity",
            "0.9",
            "--min-text-chars",
            "10",
            "--max-comments",
            "2000",
            "--max-comparisons",
            "50000",
            "--output",
            "templates.jsonl",
        ]
    )

    assert args.event_command == "template-candidates"
    assert args.window_minutes == 30
    assert args.min_similarity == 0.9
    assert args.max_comparisons == 50_000


@pytest.mark.asyncio
async def test_template_candidates_export_writes_empty_jsonl(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'templates.sqlite3'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        event = await EventRepository(session).create_event(
            slug="event-a",
            name="事件 A",
            now=datetime(2026, 7, 10, tzinfo=UTC),
        )
        await session.commit()
        assert event.id is not None
    await engine.dispose()

    output = tmp_path / "out" / "templates.jsonl"
    count = await cli._export_template_candidates(
        {"database": {"url": database_url}},
        event_reference="event-a",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        window_minutes=30,
        min_similarity=0.9,
        min_text_chars=10,
        max_comments=2000,
        max_comparisons=50_000,
        output_path=output,
    )

    assert count == 0
    assert output.read_text(encoding="utf-8") == ""
