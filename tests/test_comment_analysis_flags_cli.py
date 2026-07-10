import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base
from books_of_time.db.repositories import EventRepository


def test_refresh_comment_flags_parser_accepts_analysis_controls() -> None:
    args = cli.build_parser().parse_args(
        [
            "event",
            "refresh-comment-flags",
            "event-a",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--template-window-minutes",
            "30",
            "--template-min-similarity",
            "0.9",
            "--output",
            "flag-refresh.jsonl",
        ]
    )

    assert args.event_command == "refresh-comment-flags"
    assert args.template_window_minutes == 30
    assert args.template_min_similarity == 0.9


@pytest.mark.asyncio
async def test_refresh_comment_flags_export_writes_summary(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'flags.sqlite3'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await EventRepository(session).create_event(
            slug="event-a",
            name="事件 A",
            now=datetime(2026, 7, 10, tzinfo=UTC),
        )
        await session.commit()
    await engine.dispose()

    output = tmp_path / "out" / "refresh.jsonl"
    summary = await cli._refresh_comment_flags(
        {"database": {"url": database_url}},
        event_reference="event-a",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        template_window_minutes=30,
        template_min_similarity=0.9,
        template_min_text_chars=8,
        max_comments=2000,
        max_comparisons=50_000,
        output_path=output,
    )

    row = json.loads(output.read_text(encoding="utf-8"))
    assert summary.matched_count == 0
    assert summary.created_count == 0
    assert row["schema_version"] == "comment-flag-refresh-v1"
    assert row["event_slug"] == "event-a"
