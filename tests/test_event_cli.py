from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base
from books_of_time.db.models import CollectionTask, Event, EventTarget, EventVideo


def test_event_parser_supports_archive_management_commands() -> None:
    create = cli.build_parser().parse_args(
        [
            "event",
            "create",
            "ghost-picture-war",
            "--name",
            "鬼图战争",
            "--game",
            "Example Game",
            "--start-at",
            "2026-07-10T00:00:00+08:00",
        ]
    )
    assert create.event_command == "create"
    assert create.slug == "ghost-picture-war"
    assert create.name == "鬼图战争"

    add_target = cli.build_parser().parse_args(
        [
            "event",
            "add-target",
            "ghost-picture-war",
            "seed_bvid",
            "BV1xx411c7mD",
            "--priority",
            "90",
        ]
    )
    assert add_target.event_command == "add-target"
    assert add_target.priority == 90

    listing = cli.build_parser().parse_args(["event", "list", "--limit", "5"])
    assert listing.event_command == "list"
    assert listing.limit == 5

    videos = cli.build_parser().parse_args(
        ["event", "list-videos", "ghost-picture-war", "--limit", "10"]
    )
    assert videos.event_command == "list-videos"
    assert videos.limit == 10

    coverage = cli.build_parser().parse_args(["event", "coverage", "ghost-picture-war"])
    assert coverage.event_command == "coverage"
    assert coverage.event_reference == "ghost-picture-war"


@pytest.mark.asyncio
async def test_event_cli_helpers_create_event_and_seed_video(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite3'}"
    cfg = {"database": {"url": database_url}}
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    event = await cli._create_event(
        cfg,
        slug="ghost-picture-war",
        name="鬼图战争",
        game="Example Game",
        description=None,
        status="active",
        start_at="2026-07-10T00:00:00+08:00",
        end_at=None,
        timezone="Asia/Shanghai",
    )
    target = await cli._add_event_target(
        cfg,
        event_reference="ghost-picture-war",
        target_type="seed_bvid",
        target_value="BV1xx411c7mD",
        priority=90,
    )
    await cli._list_events(cfg, limit=10)
    await cli._list_event_videos(cfg, event_reference=str(event.id), limit=10)
    coverage = await cli._show_event_coverage(
        cfg,
        event_reference="ghost-picture-war",
    )

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        stored_event = await session.scalar(select(Event))
        video = await session.get(EventVideo, (event.id, "BV1xx411c7mD"))
        task_count = await session.scalar(select(func.count(CollectionTask.id)))
        target_count = await session.scalar(select(func.count(EventTarget.id)))
    await engine.dispose()

    assert stored_event is not None
    assert stored_event.start_at == datetime(2026, 7, 9, 16, tzinfo=UTC)
    assert target.event_id == event.id
    assert video is not None
    assert task_count == 1
    assert target_count == 1
    assert coverage.active_video_count == 1
    assert coverage.videos_with_coverage == 0


def test_parse_event_datetime_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        cli._parse_event_datetime("2026-07-10T00:00:00")
