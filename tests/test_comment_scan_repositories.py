from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.comment_scan_repositories import (
    CommentScanRunRepository,
    HotScanRunPlan,
)
from books_of_time.db.models import CommentScanRun
from books_of_time.domain.enums import CommentScanMode, CommentScanStatus


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _plan(**overrides) -> HotScanRunPlan:
    values = {
        "scan_key": "snapshot:BV-SCAN:hot_core",
        "bvid": "BV-SCAN",
        "snapshot_cohort_id": 11,
        "mode": CommentScanMode.HOT_CORE,
        "target_pages": 3,
        "start_page": 1,
        "end_page": 3,
        "policy_version": "cohort-default-v2",
        "extra": {"max_pages_per_slice": 10, "max_scan_seconds": 55},
    }
    values.update(overrides)
    return HotScanRunPlan(**values)


@pytest.mark.asyncio
async def test_materialize_hot_is_idempotent_and_preserves_identity() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CommentScanRunRepository(session)
        first, first_created = await repository.materialize_hot(_plan(), now=now)
        second, second_created = await repository.materialize_hot(
            _plan(extra={"max_pages_per_slice": 10, "max_scan_seconds": 55}),
            now=now + timedelta(seconds=1),
        )

        assert first_created is True
        assert second_created is False
        assert second.id == first.id
        assert second.status is CommentScanStatus.PLANNED
        assert second.next_page_number == 1
        assert second.target_pages == 3
        assert second.extra == {
            "max_pages_per_slice": 10,
            "max_scan_seconds": 55,
            "start_page": 1,
            "end_page": 3,
        }
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_materialize_hot_rejects_conflicting_immutable_range() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CommentScanRunRepository(session)
        scan, _ = await repository.materialize_hot(_plan(), now=now)

        with pytest.raises(ValueError, match="immutable identity"):
            await repository.materialize_hot(
                replace(_plan(), target_pages=4, end_page=4),
                now=now + timedelta(seconds=1),
            )

        await session.refresh(scan)
        assert scan.target_pages == 3
        assert scan.extra["end_page"] == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_page_progress_counts_attempts_and_advances_success_once() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CommentScanRunRepository(session)
        scan, _ = await repository.materialize_hot(_plan(), now=now)
        await repository.mark_running(scan.id, now=now, oid=777)

        await repository.record_page_requested(scan.id, page_number=1, now=now)
        await repository.record_page_succeeded(
            scan.id,
            page_number=1,
            items_observed=20,
            raw_payloads_saved=1,
            now=now + timedelta(seconds=1),
        )
        await repository.record_page_requested(
            scan.id,
            page_number=2,
            now=now + timedelta(seconds=2),
        )
        await repository.record_page_requested(
            scan.id,
            page_number=2,
            now=now + timedelta(seconds=3),
        )
        await repository.record_page_succeeded(
            scan.id,
            page_number=2,
            items_observed=12,
            raw_payloads_saved=1,
            now=now + timedelta(seconds=4),
        )

        assert scan.oid == 777
        assert scan.pages_requested == 3
        assert scan.pages_succeeded == 2
        assert scan.items_observed == 32
        assert scan.raw_payloads_saved == 2
        assert scan.next_page_number == 3

        with pytest.raises(ValueError, match="expected page 3"):
            await repository.record_page_succeeded(
                scan.id,
                page_number=2,
                items_observed=12,
                raw_payloads_saved=1,
                now=now + timedelta(seconds=5),
            )
        with pytest.raises(ValueError, match="outside configured range"):
            await repository.record_page_requested(
                scan.id,
                page_number=4,
                now=now + timedelta(seconds=5),
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_scan_status_and_outcome_transitions_are_orthogonal() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CommentScanRunRepository(session)
        completed, _ = await repository.materialize_hot(_plan(), now=now)
        await repository.mark_running(completed.id, now=now)
        await repository.mark_paused(
            completed.id,
            outcome="time_slice_yield",
            now=now + timedelta(seconds=55),
        )

        assert completed.status is CommentScanStatus.PAUSED
        assert completed.outcome == "time_slice_yield"
        assert completed.finished_at is None
        assert completed.slice_count == 1

        await repository.mark_running(
            completed.id,
            now=now + timedelta(seconds=56),
        )
        await repository.mark_complete(
            completed.id,
            outcome="server_end",
            now=now + timedelta(seconds=60),
        )

        assert completed.status is CommentScanStatus.COMPLETE
        assert completed.outcome == "server_end"
        assert completed.finished_at == now + timedelta(seconds=60)
        assert completed.slice_count == 2

        failed, _ = await repository.materialize_hot(
            _plan(scan_key="snapshot:BV-SCAN:hot_deep", mode=CommentScanMode.HOT_DEEP),
            now=now,
        )
        await repository.mark_running(failed.id, now=now)
        await repository.mark_failed(
            failed.id,
            outcome="retry_exhausted",
            error_type="transport_error",
            error_message="request failed",
            now=now + timedelta(minutes=1),
        )

        assert failed.status is CommentScanStatus.FAILED
        assert failed.outcome == "retry_exhausted"
        assert failed.last_error_type == "transport_error"
        assert failed.finished_at == now + timedelta(minutes=1)
        assert failed.truncated is True

        with pytest.raises(ValueError, match="terminal"):
            await repository.record_page_requested(
                failed.id,
                page_number=1,
                now=now + timedelta(minutes=2),
            )

    await engine.dispose()
