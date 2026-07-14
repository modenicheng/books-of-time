from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionTask,
    CommentObservation,
    CommentScanRun,
    RawPageObservation,
    SnapshotCohortComponent,
)
from books_of_time.domain.enums import (
    BilibiliRequestType,
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
    TaskStatus,
)


def test_comment_scan_enums_cover_approved_modes_and_statuses() -> None:
    assert [mode.value for mode in CommentScanMode] == [
        "hot_core",
        "hot_deep",
        "baseline_tail",
        "baseline_head_sweep",
        "incremental",
        "full_reconciliation",
        "segmented_reconciliation",
        "reply_refresh",
        "visibility_probe",
    ]


def test_snapshot_cohort_component_scan_link_is_indexed() -> None:
    assert "idx_snapshot_cohort_components_scan_run" in {
        index.name for index in SnapshotCohortComponent.__table__.indexes
    }
    assert [status.value for status in CommentScanStatus] == [
        "planned",
        "running",
        "paused",
        "complete",
        "partial",
        "failed",
        "corrupted",
    ]


@pytest.mark.asyncio
async def test_comment_scan_run_and_evidence_links_round_trip() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        scan = CommentScanRun(
            scan_key="snapshot:BV-SCAN:hot_core",
            bvid="BV-SCAN",
            oid=777,
            snapshot_cohort_id=None,
            parent_scan_run_id=None,
            mode=CommentScanMode.HOT_CORE,
            status=CommentScanStatus.PLANNED,
            outcome=None,
            target_pages=3,
            next_page_number=1,
            pages_requested=0,
            pages_succeeded=0,
            items_observed=0,
            raw_payloads_saved=0,
            slice_count=0,
            truncated=False,
            reason="routine",
            policy_version="cohort-default-v2",
            extra={"start_page": 1, "end_page": 3},
            created_at=now,
            updated_at=now,
        )
        session.add(scan)
        await session.flush()

        task = CollectionTask(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV-SCAN",
            idempotency_key="snapshot:BV-SCAN:hot_core",
            priority=100,
            budget_cost=1,
            status=TaskStatus.SUCCEEDED,
            payload={"bvid": "BV-SCAN"},
            not_before=now,
            retry_count=0,
            max_retries=3,
            comment_scan_run_id=scan.id,
            scan_slice_no=0,
            scan_slice_key=f"{scan.id}:hot_core:0",
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.flush()
        session.add_all(
            [
                CollectionCoverageStat(
                    collection_task_id=task.id,
                    comment_scan_run_id=scan.id,
                    run_id="scan-model",
                    task_kind=TaskKind.FETCH_HOT_COMMENTS,
                    target_type="video",
                    target_id="BV-SCAN",
                    started_at=now,
                    finished_at=now,
                    status="succeeded",
                ),
                RawPageObservation(
                    raw_payload_id=1,
                    scan_run_id=scan.id,
                    captured_at=now,
                    request_type=BilibiliRequestType.COMMENT_HOT,
                    target_type="video",
                    target_id="BV-SCAN",
                    page_number=1,
                    sort_mode="hot",
                    parser_version="test",
                    status="success",
                    item_count=1,
                    extra={},
                ),
                CommentObservation(
                    rpid=1001,
                    bvid="BV-SCAN",
                    oid=777,
                    scan_run_id=scan.id,
                    captured_at=now,
                    sort_mode="hot",
                    page_number=1,
                    position=1,
                    content="comment",
                    content_hash=hashlib.sha256(b"comment").digest(),
                    visibility="visible",
                    extra={},
                ),
            ]
        )
        await session.commit()

        stored = await session.scalar(select(CommentScanRun))
        stored_task = await session.scalar(select(CollectionTask))
        stored_coverage = await session.scalar(select(CollectionCoverageStat))
        stored_raw_page = await session.scalar(select(RawPageObservation))
        stored_observation = await session.scalar(select(CommentObservation))

        assert stored is not None and stored.mode is CommentScanMode.HOT_CORE
        assert stored.status is CommentScanStatus.PLANNED
        assert stored_task is not None and stored_task.comment_scan_run_id == stored.id
        assert stored_task.scan_slice_no == 0
        assert stored_coverage is not None
        assert stored_coverage.comment_scan_run_id == stored.id
        assert stored_raw_page is not None and stored_raw_page.scan_run_id == stored.id
        assert stored_observation is not None
        assert stored_observation.scan_run_id == stored.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_scan_and_slice_keys_are_unique_across_terminal_tasks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        first_scan = CommentScanRun(
            scan_key="scan-key",
            bvid="BV-SCAN",
            mode=CommentScanMode.HOT_CORE,
            status=CommentScanStatus.PLANNED,
            pages_requested=0,
            pages_succeeded=0,
            items_observed=0,
            raw_payloads_saved=0,
            slice_count=0,
            truncated=False,
            policy_version="cohort-default-v2",
            extra={},
            created_at=now,
            updated_at=now,
        )
        session.add(first_scan)
        await session.commit()
        first_scan_id = first_scan.id

        session.add(
            CommentScanRun(
                scan_key="scan-key",
                bvid="BV-OTHER",
                mode=CommentScanMode.HOT_DEEP,
                status=CommentScanStatus.PLANNED,
                pages_requested=0,
                pages_succeeded=0,
                items_observed=0,
                raw_payloads_saved=0,
                slice_count=0,
                truncated=False,
                policy_version="cohort-default-v2",
                extra={},
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

        first_task = CollectionTask(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV-SCAN",
            priority=100,
            budget_cost=1,
            status=TaskStatus.SUCCEEDED,
            payload={},
            not_before=now,
            retry_count=0,
            max_retries=3,
            comment_scan_run_id=first_scan_id,
            scan_slice_no=0,
            scan_slice_key=f"{first_scan_id}:hot_core:0",
            created_at=now,
            updated_at=now,
        )
        session.add(first_task)
        await session.commit()

        session.add(
            CollectionTask(
                kind=TaskKind.FETCH_HOT_COMMENTS,
                target_type="video",
                target_id="BV-SCAN",
                priority=100,
                budget_cost=1,
                status=TaskStatus.PENDING,
                payload={},
                not_before=now,
                retry_count=0,
                max_retries=3,
                comment_scan_run_id=first_scan_id,
                scan_slice_no=0,
                scan_slice_key=f"{first_scan_id}:hot_core:0",
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    await engine.dispose()
