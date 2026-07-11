import json
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

    update = cli.build_parser().parse_args(
        [
            "event",
            "update",
            "ghost-picture-war",
            "--name",
            "鬼图战争复盘",
            "--clear-game",
            "--status",
            "closed",
            "--end-at",
            "2026-07-18T00:00:00+08:00",
        ]
    )
    assert update.event_command == "update"
    assert update.clear_game is True
    assert update.status == "closed"

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

    targets = cli.build_parser().parse_args(
        [
            "event",
            "list-targets",
            "ghost-picture-war",
            "--type",
            "keyword",
            "--all",
        ]
    )
    assert targets.event_command == "list-targets"
    assert targets.target_type == "keyword"
    assert targets.all is True

    target_status = cli.build_parser().parse_args(
        [
            "event",
            "set-target-status",
            "ghost-picture-war",
            "42",
            "inactive",
        ]
    )
    assert target_status.target_id == 42
    assert target_status.status == "inactive"

    video_status = cli.build_parser().parse_args(
        [
            "event",
            "set-video-status",
            "ghost-picture-war",
            "BV1xx411c7mD",
            "active",
        ]
    )
    assert video_status.bvid == "BV1xx411c7mD"
    assert video_status.status == "active"

    official_target = cli.build_parser().parse_args(
        [
            "event",
            "add-target",
            "ghost-picture-war",
            "uid",
            "12345",
            "--role",
            "official",
        ]
    )
    assert official_target.role == "official"

    listing = cli.build_parser().parse_args(["event", "list", "--limit", "5"])
    assert listing.event_command == "list"
    assert listing.limit == 5

    videos = cli.build_parser().parse_args(
        ["event", "list-videos", "ghost-picture-war", "--limit", "10"]
    )
    assert videos.event_command == "list-videos"
    assert videos.limit == 10
    assert videos.all is False

    coverage = cli.build_parser().parse_args(["event", "coverage", "ghost-picture-war"])
    assert coverage.event_command == "coverage"
    assert coverage.event_reference == "ghost-picture-war"

    export = cli.build_parser().parse_args(
        [
            "event",
            "export-timeline",
            "ghost-picture-war",
            "--output",
            "timeline.jsonl",
        ]
    )
    assert export.event_command == "export-timeline"
    assert export.output == "timeline.jsonl"

    trends = cli.build_parser().parse_args(
        [
            "event",
            "keyword-trends",
            "ghost-picture-war",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--bucket-minutes",
            "60",
            "--output",
            "trends.jsonl",
        ]
    )
    assert trends.event_command == "keyword-trends"
    assert trends.bucket_minutes == 60

    cooccurrence = cli.build_parser().parse_args(
        [
            "event",
            "keyword-cooccurrence",
            "ghost-picture-war",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--output",
            "cooccurrence.jsonl",
        ]
    )
    assert cooccurrence.event_command == "keyword-cooccurrence"

    stance = cli.build_parser().parse_args(
        [
            "event",
            "stance-evidence",
            "ghost-picture-war",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--output",
            "stance.jsonl",
        ]
    )
    assert stance.event_command == "stance-evidence"


@pytest.mark.asyncio
async def test_event_cli_helpers_create_event_and_seed_video(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite3'}"
    cfg = {
        "database": {"url": database_url},
        "analysis": {
            "stance_lexicon": {
                "version": "test-v1",
                "support": ["赞同"],
                "criticism": ["质疑"],
                "neutral": ["观望"],
            }
        },
    }
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
    await cli._add_event_target(
        cfg,
        event_reference="ghost-picture-war",
        target_type="keyword",
        target_value="控评",
        priority=50,
    )
    official_target = await cli._add_event_target(
        cfg,
        event_reference="ghost-picture-war",
        target_type="uid",
        target_value="12345",
        priority=100,
        role="official",
    )
    await cli._add_event_target(
        cfg,
        event_reference="ghost-picture-war",
        target_type="keyword",
        target_value="删评",
        priority=50,
    )
    await cli._list_events(cfg, limit=10)
    await cli._list_event_videos(cfg, event_reference=str(event.id), limit=10)
    coverage = await cli._show_event_coverage(
        cfg,
        event_reference="ghost-picture-war",
    )
    output_path = tmp_path / "exports" / "timeline.jsonl"
    exported = await cli._export_event_timeline(
        cfg,
        event_reference="ghost-picture-war",
        output_path=output_path,
    )
    trend_path = tmp_path / "exports" / "trends.jsonl"
    trend_count = await cli._export_keyword_trends(
        cfg,
        event_reference="ghost-picture-war",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        bucket_minutes=60,
        bvid=None,
        output_path=trend_path,
    )
    cooccurrence_path = tmp_path / "exports" / "cooccurrence.jsonl"
    cooccurrence_count = await cli._export_keyword_cooccurrence(
        cfg,
        event_reference="ghost-picture-war",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        bvid=None,
        output_path=cooccurrence_path,
    )
    stance_path = tmp_path / "exports" / "stance.jsonl"
    stance_count = await cli._export_stance_evidence(
        cfg,
        event_reference="ghost-picture-war",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        bvid=None,
        output_path=stance_path,
    )
    updated = await cli._update_event(
        cfg,
        event_reference="ghost-picture-war",
        name="鬼图战争复盘",
        game=None,
        clear_game=True,
        description="归档说明",
        clear_description=False,
        status="closed",
        start_at=None,
        clear_start_at=False,
        end_at="2026-07-18T00:00:00+08:00",
        clear_end_at=False,
        timezone="UTC",
    )
    targets = await cli._list_event_targets(
        cfg,
        event_reference="ghost-picture-war",
        target_type=None,
        include_inactive=True,
        limit=10,
    )
    disabled_target = await cli._set_event_target_active(
        cfg,
        event_reference="ghost-picture-war",
        target_id=target.id,
        active=False,
    )
    videos = await cli._list_event_videos(
        cfg,
        event_reference="ghost-picture-war",
        include_inactive=True,
        limit=10,
    )
    disabled_video = await cli._set_event_video_active(
        cfg,
        event_reference="ghost-picture-war",
        bvid="BV1xx411c7mD",
        active=False,
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
    assert updated.slug == "ghost-picture-war"
    assert updated.name == "鬼图战争复盘"
    assert updated.game is None
    assert updated.status == "closed"
    assert target.event_id == event.id
    assert len(targets) == 4
    assert disabled_target.active is False
    assert [item.bvid for item in videos] == ["BV1xx411c7mD"]
    assert disabled_video.active is False
    assert video is not None
    assert task_count == 1
    assert target_count == 4
    assert official_target.extra == {"role": "official"}
    assert coverage.active_video_count == 1
    assert coverage.videos_with_coverage == 0
    assert exported == 1
    exported_rows = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert exported_rows[0]["record_type"] == "event_video_associated"
    assert exported_rows[0]["bvid"] == "BV1xx411c7mD"
    assert trend_count == 4
    trend_rows = [
        json.loads(line) for line in trend_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["distinct_comment_count"] for row in trend_rows] == [0, 0, 0, 0]
    assert {row["keyword"] for row in trend_rows} == {"控评", "删评"}
    assert cooccurrence_count == 0
    assert cooccurrence_path.read_text(encoding="utf-8") == ""
    assert stance_count == 3
    stance_rows = [
        json.loads(line)
        for line in stance_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["category"] for row in stance_rows] == [
        "support",
        "criticism",
        "neutral",
    ]
    assert {row["lexicon_version"] for row in stance_rows} == {"test-v1"}


def test_parse_event_datetime_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        cli._parse_event_datetime("2026-07-10T00:00:00")
