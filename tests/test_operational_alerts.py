from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import CollectionCoverageStat, OperationalAlertState
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    ScheduledJobRepository,
    ServiceInstanceRepository,
)
from books_of_time.domain.enums import ScheduledJobKind, TaskKind, TaskStatus
from books_of_time.service.alerts import (
    AlertTransition,
    OperationalAlertEvaluator,
    OperationalAlertPolicy,
)


class RecordingNotifier:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str]] = []

    async def notify(self, transition: AlertTransition) -> None:
        self.transitions.append((transition.action, transition.state.alert_key))


@pytest.mark.asyncio
async def test_operational_alerts_deduplicate_and_record_recovery() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    notifier = RecordingNotifier()
    evaluator = OperationalAlertEvaluator(
        policy=OperationalAlertPolicy(
            worker_heartbeat_timeout_seconds=60,
            pending_task_threshold=1,
            oldest_pending_seconds=60,
            request_failure_window_seconds=3600,
            request_failure_min_pages=1,
            request_failure_rate=0.2,
            scheduled_job_failure_threshold=2,
            repeat_notification_seconds=3600,
        ),
        notifier=notifier,
    )

    async with factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV1xx411c7mD",
            priority=80,
            payload={"bvid": "BV1xx411c7mD"},
            not_before=now - timedelta(hours=1),
        )
        task.created_at = now - timedelta(hours=1)
        job = await ScheduledJobRepository(session).ensure(
            job_key="uid-discovery",
            job_kind=ScheduledJobKind.UID_DISCOVERY,
            schedule_seconds=60,
            priority=100,
            payload={},
            next_run_at=now,
        )
        job.consecutive_failures = 3
        session.add(_coverage(now - timedelta(minutes=1), pages=10, errors=5))
        await session.commit()

        first = await evaluator.evaluate(session, now=now)
        await session.commit()
        second = await evaluator.evaluate(session, now=now + timedelta(seconds=30))
        await session.commit()

        states = list(
            await session.scalars(
                select(OperationalAlertState).order_by(OperationalAlertState.alert_key)
            )
        )
        assert first.triggered_count == 4
        assert first.notification_count == 4
        assert second.triggered_count == 4
        assert second.notification_count == 0
        assert len(states) == 4
        assert {state.status for state in states} == {"active"}
        assert {state.occurrence_count for state in states} == {2}
        assert notifier.transitions == [
            ("triggered", "request_failure_rate"),
            ("triggered", "scheduled_job_failure:uid-discovery"),
            ("triggered", "task_backlog"),
            ("triggered", "worker_heartbeat"),
        ]

        task.status = TaskStatus.SUCCEEDED
        job.consecutive_failures = 0
        await ServiceInstanceRepository(session).register(
            instance_id="worker-1",
            hostname="worker-host",
            pid=123,
            version="0.1.0",
            roles=["worker"],
            now=now + timedelta(minutes=2),
        )
        await ServiceInstanceRepository(session).mark_running(
            "worker-1",
            now=now + timedelta(minutes=2),
        )
        session.add(
            _coverage(
                now + timedelta(minutes=1),
                pages=100,
                errors=0,
                task_id=2,
            )
        )
        await session.commit()

        recovered = await evaluator.evaluate(
            session,
            now=now + timedelta(minutes=2),
        )
        await session.commit()
        states = list(await session.scalars(select(OperationalAlertState)))

        assert recovered.triggered_count == 0
        assert recovered.resolved_count == 4
        assert recovered.notification_count == 4
        assert {state.status for state in states} == {"resolved"}
        assert all(state.resolved_at == now + timedelta(minutes=2) for state in states)
        assert notifier.transitions[-4:] == [
            ("resolved", "request_failure_rate"),
            ("resolved", "scheduled_job_failure:uid-discovery"),
            ("resolved", "task_backlog"),
            ("resolved", "worker_heartbeat"),
        ]

    await engine.dispose()


def test_operational_alert_policy_validates_thresholds() -> None:
    with pytest.raises(ValueError, match="request_failure_rate"):
        OperationalAlertPolicy(request_failure_rate=1.1)
    with pytest.raises(ValueError, match="threshold"):
        OperationalAlertPolicy(pending_task_threshold=0)


def _coverage(
    finished_at: datetime,
    *,
    pages: int,
    errors: int,
    task_id: int = 1,
) -> CollectionCoverageStat:
    return CollectionCoverageStat(
        collection_task_id=task_id,
        run_id=f"alert-run-{task_id}",
        task_kind=TaskKind.FETCH_HOT_COMMENTS,
        target_type="video",
        target_id="BV1xx411c7mD",
        started_at=finished_at - timedelta(seconds=5),
        finished_at=finished_at,
        status="failed" if errors else "succeeded",
        pages_requested=pages,
        pages_succeeded=pages - errors,
        items_observed=0,
        raw_payloads_saved=1,
        parse_errors=0,
        request_errors=errors,
        frontier_reached=None,
        frontier_missing=None,
        truncated=False,
        corrupted=False,
        reason=None,
        extra={},
        created_at=finished_at,
        updated_at=finished_at,
    )
