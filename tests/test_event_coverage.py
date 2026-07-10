from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import CollectionCoverageStat
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import TaskKind


@pytest.mark.asyncio
async def test_event_coverage_aggregates_only_active_event_videos() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="event-a",
            name="事件 A",
            now=now,
        )
        for bvid in ("BV1xx411c7mD", "BV1Q541167Qg", "BV1mK4y1C7Bz"):
            await repository.attach_video(
                event_id=event.id,
                bvid=bvid,
                association_reason="manual",
                now=now,
            )
        session.add_all(
            [
                _coverage(
                    task_id=1,
                    bvid="BV1xx411c7mD",
                    status="succeeded",
                    started_at=now,
                    pages_requested=2,
                    pages_succeeded=2,
                    items_observed=40,
                    raw_payloads_saved=2,
                ),
                _coverage(
                    task_id=2,
                    bvid="BV1xx411c7mD",
                    status="partial",
                    started_at=now + timedelta(minutes=1),
                    pages_requested=3,
                    pages_succeeded=2,
                    items_observed=30,
                    raw_payloads_saved=2,
                    parse_errors=1,
                    truncated=True,
                ),
                _coverage(
                    task_id=3,
                    bvid="BV1Q541167Qg",
                    status="failed",
                    started_at=now + timedelta(minutes=2),
                    pages_requested=1,
                    pages_succeeded=0,
                    raw_payloads_saved=1,
                    request_errors=2,
                    corrupted=True,
                ),
                _coverage(
                    task_id=4,
                    bvid="BV1outside00",
                    status="succeeded",
                    started_at=now,
                    pages_requested=100,
                    pages_succeeded=100,
                    raw_payloads_saved=100,
                ),
            ]
        )
        await session.commit()

        summary = await repository.get_coverage_summary(event.id)

    assert summary.event_id == event.id
    assert summary.event_slug == "event-a"
    assert summary.active_video_count == 3
    assert summary.videos_with_coverage == 2
    assert summary.coverage_row_count == 3
    assert summary.succeeded_count == 1
    assert summary.partial_count == 1
    assert summary.failed_count == 1
    assert summary.pages_requested == 6
    assert summary.pages_succeeded == 4
    assert summary.items_observed == 70
    assert summary.raw_payloads_saved == 5
    assert summary.parse_errors == 1
    assert summary.request_errors == 2
    assert summary.truncated_count == 1
    assert summary.corrupted_count == 1
    assert summary.video_coverage_ratio == pytest.approx(2 / 3)
    assert summary.page_success_rate == pytest.approx(2 / 3)
    assert summary.first_started_at == now
    assert summary.last_finished_at == now + timedelta(minutes=2, seconds=5)
    await engine.dispose()


@pytest.mark.asyncio
async def test_event_coverage_handles_empty_event() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="empty-event",
            name="空事件",
            now=now,
        )
        summary = await repository.get_coverage_summary("empty-event")

    assert summary.event_id == event.id
    assert summary.active_video_count == 0
    assert summary.videos_with_coverage == 0
    assert summary.video_coverage_ratio is None
    assert summary.page_success_rate is None
    assert summary.first_started_at is None
    assert summary.last_finished_at is None
    await engine.dispose()


def _coverage(
    *,
    task_id: int,
    bvid: str,
    status: str,
    started_at: datetime,
    pages_requested: int,
    pages_succeeded: int,
    raw_payloads_saved: int,
    items_observed: int = 0,
    parse_errors: int = 0,
    request_errors: int = 0,
    truncated: bool = False,
    corrupted: bool = False,
) -> CollectionCoverageStat:
    finished_at = started_at + timedelta(seconds=5)
    return CollectionCoverageStat(
        collection_task_id=task_id,
        run_id=f"run-{task_id}",
        task_kind=TaskKind.FETCH_HOT_COMMENTS,
        target_type="video",
        target_id=bvid,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        pages_requested=pages_requested,
        pages_succeeded=pages_succeeded,
        items_observed=items_observed,
        raw_payloads_saved=raw_payloads_saved,
        parse_errors=parse_errors,
        request_errors=request_errors,
        frontier_reached=None,
        frontier_missing=None,
        truncated=truncated,
        corrupted=corrupted,
        reason=None,
        extra={},
        created_at=finished_at,
        updated_at=finished_at,
    )
