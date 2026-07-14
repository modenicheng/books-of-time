from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.latest_scan_repositories import (
    LatestScanRunPlan,
    LatestScanRunRepository,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CommentScanRun,
    KnownVideo,
)
from books_of_time.db.repositories import (
    FrontierStateRepository,
    FrontierStateUpdate,
    FrontierVersionConflict,
)
from books_of_time.domain.enums import CommentScanMode, CommentScanStatus


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    async with session_factory.begin() as session:
        session.add(
            CollectionPolicyVersion(
                version="cohort-default-v2",
                policy_kind="snapshot_cohort",
                scope_type="global",
                scope_id="global",
                timezone="Asia/Shanghai",
                policy={},
                algorithm="configured-fixed-v1",
                created_at=now,
                activated_at=now,
                active=True,
            )
        )
        session.add_all(
            [
                KnownVideo(
                    bvid=bvid,
                    source_mid="42",
                    pubdate=now - timedelta(hours=1),
                    first_seen_at=now - timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
                for bvid in ("BV-LATEST", "BV-OTHER")
            ]
        )
    return engine, session_factory


def _plan(**overrides) -> LatestScanRunPlan:
    values = {
        "scan_key": "snapshot:BV-LATEST:latest:1",
        "bvid": "BV-LATEST",
        "snapshot_cohort_id": None,
        "parent_scan_run_id": None,
        "mode": CommentScanMode.BASELINE_TAIL,
        "policy_version": "cohort-default-v2",
        "reason": "routine",
        "start_frontier_rpid": None,
        "start_anchor_set": [],
        "start_cursor": None,
        "extra": {"max_scan_seconds": 55},
    }
    values.update(overrides)
    return LatestScanRunPlan(**values)


def _update(state, **overrides) -> FrontierStateUpdate:
    values = {
        "frontier_rpid": state.frontier_rpid,
        "frontier_time": state.frontier_time,
        "frontier_anchor_set": state.frontier_anchor_set,
        "active_scan_run_id": state.active_scan_run_id,
        "cursor": state.cursor,
        "last_scan_at": state.last_scan_at,
        "last_scan_status": state.last_scan_status,
        "last_scan_pages": state.last_scan_pages,
        "last_scan_truncated": state.last_scan_truncated,
        "extra": state.extra,
    }
    values.update(overrides)
    return FrontierStateUpdate(**values)


@pytest.mark.asyncio
async def test_frontier_get_or_create_is_idempotent_and_lockable() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory.begin() as session:
        repository = FrontierStateRepository(session)
        first = await repository.get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now,
            lock=True,
        )
        second = await repository.get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now + timedelta(seconds=1),
            lock=True,
        )

        assert second.id == first.id
        assert second.version == 0
        assert second.frontier_anchor_set == []
        assert second.active_scan_run_id is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_frontier_compare_and_swap_replaces_snapshot_once() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    anchor_time = now - timedelta(minutes=5)
    anchors = [{"rpid": 1001, "platform_created_at": anchor_time.isoformat()}]

    async with session_factory.begin() as session:
        repository = FrontierStateRepository(session)
        state = await repository.get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now,
        )
        updated = await repository.compare_and_swap(
            state.id,
            0,
            _update(
                state,
                frontier_rpid=1001,
                frontier_time=anchor_time,
                frontier_anchor_set=anchors,
                cursor="offset-2",
                last_scan_at=now,
                last_scan_status="paused",
                last_scan_pages=2,
                last_scan_truncated=True,
                extra={"cursor_attempts": 1},
            ),
            now=now,
        )

        assert updated.version == 1
        assert updated.frontier_rpid == 1001
        assert updated.frontier_anchor_set == anchors
        assert updated.cursor == "offset-2"
        assert updated.extra == {"cursor_attempts": 1}

        stale_update = _update(
            updated,
            cursor="stale-offset",
            extra={"cursor_attempts": 99},
        )
        with pytest.raises(FrontierVersionConflict):
            await repository.compare_and_swap(
                state.id,
                0,
                stale_update,
                now=now + timedelta(seconds=1),
            )

        await session.refresh(updated)
        assert updated.version == 1
        assert updated.cursor == "offset-2"
        assert updated.extra == {"cursor_attempts": 1}

    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_or_join_creates_one_active_latest_scan() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory.begin() as session:
        frontier_repository = FrontierStateRepository(session)
        state = await frontier_repository.get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now,
        )
        repository = LatestScanRunRepository(session)
        first = await repository.claim_or_join(
            _plan(),
            frontier_state=state,
            expected_version=state.version,
            now=now,
        )

        assert first.created is True
        assert first.scan.mode is CommentScanMode.BASELINE_TAIL
        assert first.scan.status is CommentScanStatus.PLANNED
        assert first.frontier_state.active_scan_run_id == first.scan.id
        assert first.frontier_state.version == 1

        second = await repository.claim_or_join(
            _plan(
                scan_key="snapshot:BV-LATEST:latest:2",
                mode=CommentScanMode.INCREMENTAL,
            ),
            frontier_state=first.frontier_state,
            expected_version=first.frontier_state.version,
            now=now + timedelta(seconds=1),
        )

        assert second.created is False
        assert second.scan.id == first.scan.id
        assert second.frontier_state.version == 1
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 1

        with pytest.raises(ValueError, match="immutable identity"):
            await repository.claim_or_join(
                _plan(extra={"max_scan_seconds": 10}),
                frontier_state=second.frontier_state,
                expected_version=second.frontier_state.version,
                now=now + timedelta(seconds=2),
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_replaces_terminal_pointer_but_rejects_cross_bvid_owner() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory.begin() as session:
        frontier_repository = FrontierStateRepository(session)
        state = await frontier_repository.get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now,
        )
        repository = LatestScanRunRepository(session)
        first = await repository.claim_or_join(
            _plan(),
            frontier_state=state,
            expected_version=0,
            now=now,
        )
        first.scan.status = CommentScanStatus.COMPLETE
        first.scan.outcome = "tail_reached"
        first.scan.finished_at = now + timedelta(seconds=1)
        await session.flush()
        first_version = first.frontier_state.version

        replacement = await repository.claim_or_join(
            _plan(
                scan_key="snapshot:BV-LATEST:latest:replacement",
                mode=CommentScanMode.BASELINE_HEAD_SWEEP,
                parent_scan_run_id=first.scan.id,
            ),
            frontier_state=first.frontier_state,
            expected_version=first.frontier_state.version,
            now=now + timedelta(seconds=2),
        )

        assert replacement.created is True
        assert replacement.scan.id != first.scan.id
        assert replacement.frontier_state.active_scan_run_id == replacement.scan.id
        assert replacement.frontier_state.version > first_version

        other_state = await frontier_repository.get_or_create(
            target_type="video",
            target_id="BV-OTHER",
            frontier_type="latest_comments",
            now=now,
        )
        other_claim = await repository.claim_or_join(
            _plan(
                scan_key="snapshot:BV-OTHER:latest:1",
                bvid="BV-OTHER",
            ),
            frontier_state=other_state,
            expected_version=other_state.version,
            now=now,
        )
        crossed = await frontier_repository.compare_and_swap(
            replacement.frontier_state.id,
            replacement.frontier_state.version,
            _update(
                replacement.frontier_state,
                active_scan_run_id=other_claim.scan.id,
            ),
            now=now + timedelta(seconds=3),
        )

        with pytest.raises(ValueError, match="different BVID"):
            await repository.claim_or_join(
                _plan(scan_key="snapshot:BV-LATEST:latest:crossed"),
                frontier_state=crossed,
                expected_version=crossed.version,
                now=now + timedelta(seconds=4),
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_scan_progress_is_monotonic_and_terminal_is_immutable() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    anchors = [{"rpid": 2001, "platform_created_at": now.isoformat()}]

    async with session_factory.begin() as session:
        frontier_repository = FrontierStateRepository(session)
        state = await frontier_repository.get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now,
        )
        repository = LatestScanRunRepository(session)
        claim = await repository.claim_or_join(
            _plan(),
            frontier_state=state,
            expected_version=0,
            now=now,
        )
        scan = await repository.mark_running(claim.scan.id, now=now)

        with pytest.raises(ValueError, match="recorded request"):
            await repository.record_page_succeeded(
                scan.id,
                result_cursor="offset-2",
                result_anchor_set=anchors,
                items_observed=20,
                raw_payloads_saved=1,
                now=now + timedelta(seconds=1),
            )

        await repository.record_page_requested(
            scan.id,
            now=now + timedelta(seconds=2),
        )
        await repository.record_page_succeeded(
            scan.id,
            result_cursor="offset-2",
            result_anchor_set=anchors,
            items_observed=20,
            raw_payloads_saved=1,
            now=now + timedelta(seconds=3),
        )

        assert scan.pages_requested == 1
        assert scan.pages_succeeded == 1
        assert scan.items_observed == 20
        assert scan.raw_payloads_saved == 1
        assert scan.result_cursor == "offset-2"
        assert scan.result_frontier_rpid == 2001
        assert scan.result_anchor_set == anchors
        assert scan.slice_count == 1

        with pytest.raises(ValueError, match="recorded request"):
            await repository.record_page_succeeded(
                scan.id,
                result_cursor="offset-2",
                result_anchor_set=anchors,
                items_observed=20,
                raw_payloads_saved=1,
                now=now + timedelta(seconds=4),
            )

        await repository.mark_complete(
            scan.id,
            outcome="frontier_reached",
            now=now + timedelta(seconds=5),
        )
        with pytest.raises(ValueError, match="terminal"):
            await repository.mark_running(
                scan.id,
                now=now + timedelta(seconds=6),
            )
        with pytest.raises(ValueError, match="terminal"):
            await repository.record_page_requested(
                scan.id,
                now=now + timedelta(seconds=6),
            )

    await engine.dispose()


@pytest.mark.parametrize(
    "plan",
    [
        _plan(mode=CommentScanMode.HOT_CORE),
        _plan(start_anchor_set=[{"rpid": -1, "platform_created_at": None}]),
        _plan(
            start_frontier_rpid=1002,
            start_anchor_set=[{"rpid": 1001, "platform_created_at": None}],
        ),
    ],
)
@pytest.mark.asyncio
async def test_claim_rejects_invalid_latest_plan(plan: LatestScanRunPlan) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory.begin() as session:
        state = await FrontierStateRepository(session).get_or_create(
            target_type="video",
            target_id="BV-LATEST",
            frontier_type="latest_comments",
            now=now,
        )
        with pytest.raises(ValueError):
            await LatestScanRunRepository(session).claim_or_join(
                plan,
                frontier_state=state,
                expected_version=state.version,
                now=now,
            )

    await engine.dispose()
