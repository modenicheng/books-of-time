from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionCoverageStat,
    OperationalAlertState,
    RequestBackoffState,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    ServiceInstanceRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.service.health import ServiceHealthChecker


async def _build_checker(
    tmp_path: Path,
    *,
    raw_dir: Path | None = None,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    checker = ServiceHealthChecker(
        session_factory=session_factory,
        raw_dir=raw_dir or tmp_path / "raw",
        media_dir=tmp_path / "media",
        heartbeat_timeout_seconds=30,
    )
    return engine, session_factory, checker


@pytest.mark.asyncio
async def test_service_doctor_checks_database_and_storage(tmp_path: Path) -> None:
    engine, _, checker = await _build_checker(tmp_path)

    report = await checker.doctor()

    assert report.ok is True
    assert {check.name for check in report.checks} == {
        "database",
        "raw_storage",
        "media_storage",
    }
    assert all(check.ok for check in report.checks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_health_requires_fresh_running_heartbeat(tmp_path: Path) -> None:
    engine, session_factory, checker = await _build_checker(tmp_path)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)

    without_instance = await checker.health(now=now)
    heartbeat_check = next(
        check for check in without_instance.checks if check.name == "service_heartbeat"
    )
    assert without_instance.ok is False
    assert heartbeat_check.ok is False

    async with session_factory() as session:
        repo = ServiceInstanceRepository(session)
        await repo.register(
            instance_id="service-health",
            hostname="collector-host",
            pid=321,
            version="0.1.0",
            roles=["worker"],
            now=now,
        )
        await repo.mark_running("service-health", now=now)
        await session.commit()

    with_instance = await checker.health(now=now + timedelta(seconds=10))
    assert with_instance.ok is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_health_requires_fresh_worker_role(tmp_path: Path) -> None:
    engine, session_factory, checker = await _build_checker(tmp_path)
    now = datetime(2026, 7, 10, 2, 30, tzinfo=UTC)

    async with session_factory() as session:
        repo = ServiceInstanceRepository(session)
        await repo.register(
            instance_id="scheduler-only",
            hostname="collector-host",
            pid=322,
            version="0.1.0",
            roles=["scheduler"],
            now=now,
        )
        await repo.mark_running("scheduler-only", now=now)
        await session.commit()

    report = await checker.health(now=now + timedelta(seconds=10))
    service = next(
        check for check in report.checks if check.name == "service_heartbeat"
    )
    worker = next(check for check in report.checks if check.name == "worker_heartbeat")

    assert service.ok is True
    assert worker.ok is False
    assert report.ok is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_doctor_reports_unwritable_storage_without_raising(
    tmp_path: Path,
) -> None:
    raw_file = tmp_path / "not-a-directory"
    raw_file.write_text("occupied", encoding="utf-8")
    engine, _, checker = await _build_checker(tmp_path, raw_dir=raw_file)

    report = await checker.doctor()
    raw_check = next(check for check in report.checks if check.name == "raw_storage")

    assert report.ok is False
    assert raw_check.ok is False
    assert "FileExistsError" in raw_check.detail
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_doctor_probes_configured_raw_backend(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    raw_store = _ProbeRawStore()
    checker = ServiceHealthChecker(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        raw_store=raw_store,
        media_dir=tmp_path / "media",
    )

    report = await checker.doctor()
    raw_check = next(check for check in report.checks if check.name == "raw_storage")

    assert raw_check.ok is True
    assert raw_check.detail == "remote raw bucket reachable"
    assert raw_store.probe_count == 1
    await engine.dispose()


class _ProbeRawStore:
    def __init__(self) -> None:
        self.probe_count = 0

    def probe(self) -> str:
        self.probe_count += 1
        return "remote raw bucket reachable"


@pytest.mark.asyncio
async def test_service_status_summarizes_instances_tasks_and_backoffs(
    tmp_path: Path,
) -> None:
    engine, session_factory, checker = await _build_checker(tmp_path)
    now = datetime(2026, 7, 10, 3, 0, tzinfo=UTC)

    async with session_factory() as session:
        instance_repo = ServiceInstanceRepository(session)
        await instance_repo.register(
            instance_id="service-status",
            hostname="collector-host",
            pid=456,
            version="0.1.0",
            roles=["worker"],
            now=now,
        )
        await instance_repo.mark_running("service-status", now=now)

        task_repo = CollectionTaskRepository(session)
        pending = await task_repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV-pending",
            priority=10,
            payload={},
            not_before=now,
        )
        pending.created_at = now - timedelta(minutes=5)
        running = await task_repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV-running",
            priority=10,
            payload={},
            not_before=now,
        )
        running.status = TaskStatus.RUNNING
        failed = await task_repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV-failed",
            priority=10,
            payload={},
            not_before=now,
        )
        failed.status = TaskStatus.FAILED
        session.add(
            RequestBackoffState(
                platform="bilibili",
                request_type=BilibiliRequestType.VIDEO_STATS,
                scope="global",
                error_kind="429",
                status_code=429,
                retry_after_seconds=60,
                fail_count=1,
                first_failed_at=now,
                last_failed_at=now,
                backoff_until=now + timedelta(minutes=1),
                last_message="rate limited",
                extra={},
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                _coverage_stat(
                    run_id="recent",
                    now=now - timedelta(minutes=10),
                    pages_requested=10,
                    request_errors=2,
                    parse_errors=1,
                ),
                _coverage_stat(
                    run_id="old",
                    now=now - timedelta(hours=2),
                    pages_requested=10,
                    request_errors=9,
                    parse_errors=3,
                ),
            ]
        )
        session.add(
            OperationalAlertState(
                alert_key="task_backlog",
                alert_type="task_backlog",
                severity="warning",
                status="active",
                summary="Collection task backlog exceeded its threshold",
                details={"pending_count": 1},
                first_triggered_at=now - timedelta(minutes=2),
                last_evaluated_at=now,
                last_triggered_at=now,
                last_notified_at=now,
                resolved_at=None,
                occurrence_count=2,
                created_at=now - timedelta(minutes=2),
                updated_at=now,
            )
        )
        await session.commit()

    status = await checker.status(now=now, instance_limit=5)

    assert len(status.instances) == 1
    assert status.instances[0].instance_id == "service-status"
    assert status.pending_tasks == 1
    assert status.running_tasks == 1
    assert status.failed_tasks == 1
    assert status.oldest_pending_at == now - timedelta(minutes=5)
    assert status.active_backoffs == 1
    assert status.request_failures.coverage_runs == 1
    assert status.request_failures.pages_requested == 10
    assert status.request_failures.request_errors == 2
    assert status.request_failures.parse_errors == 1
    assert status.request_failures.request_failure_rate == 0.2
    assert len(status.active_alerts) == 1
    assert status.active_alerts[0].alert_key == "task_backlog"
    assert status.active_alerts[0].severity == "warning"
    assert status.active_alerts[0].occurrence_count == 2
    await engine.dispose()


def _coverage_stat(
    *,
    run_id: str,
    now: datetime,
    pages_requested: int,
    request_errors: int,
    parse_errors: int,
) -> CollectionCoverageStat:
    return CollectionCoverageStat(
        collection_task_id=1,
        run_id=run_id,
        task_kind=TaskKind.FETCH_VIDEO_STATS,
        target_type="video",
        target_id=f"BV-{run_id}",
        started_at=now - timedelta(seconds=5),
        finished_at=now,
        status="succeeded",
        pages_requested=pages_requested,
        pages_succeeded=pages_requested - request_errors,
        items_observed=0,
        raw_payloads_saved=0,
        parse_errors=parse_errors,
        request_errors=request_errors,
        frontier_reached=None,
        frontier_missing=None,
        truncated=False,
        corrupted=False,
        reason=None,
        extra={},
        created_at=now,
        updated_at=now,
    )
