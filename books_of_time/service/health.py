from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.migrations import get_current_schema_revision
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionTask,
    OperationalAlertState,
    RequestBackoffState,
    ServiceInstance,
)
from books_of_time.db.repositories import ServiceInstanceRepository
from books_of_time.domain.enums import TaskStatus
from books_of_time.service.models import (
    OperationalAlertSummary,
    RequestFailureWindow,
    ServiceCheck,
    ServiceHealthReport,
    ServiceInstanceSummary,
    ServiceStatusSnapshot,
)
from books_of_time.storage.base import RawPayloadStore
from books_of_time.storage.filesystem import RawPayloadFileStore


class ServiceHealthChecker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        media_dir: str | Path,
        raw_dir: str | Path | None = None,
        raw_store: RawPayloadStore | None = None,
        heartbeat_timeout_seconds: float = 30,
        request_failure_window_seconds: int = 3600,
        expected_schema_revision: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        if raw_store is None and raw_dir is None:
            raise ValueError("raw_dir is required when raw_store is not provided")
        self.raw_store = raw_store or RawPayloadFileStore(raw_dir)
        self.media_dir = Path(media_dir)
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        if not 60 <= request_failure_window_seconds <= 604_800:
            raise ValueError(
                "request_failure_window_seconds must be between 60 and 604800"
            )
        self.request_failure_window_seconds = request_failure_window_seconds
        self.expected_schema_revision = expected_schema_revision

    async def doctor(self) -> ServiceHealthReport:
        checks = [await self._database_check()]
        if self.expected_schema_revision is not None:
            checks.append(await self._schema_revision_check())
        checks.append(await self._raw_storage_check())
        checks.append(self._storage_check("media_storage", self.media_dir))
        return ServiceHealthReport(tuple(checks))

    async def health(self, *, now: datetime) -> ServiceHealthReport:
        doctor = await self.doctor()
        try:
            async with self.session_factory() as session:
                repository = ServiceInstanceRepository(session)
                is_fresh = await repository.has_fresh_running_instance(
                    now=now,
                    timeout_seconds=self.heartbeat_timeout_seconds,
                )
                worker_is_fresh = await repository.has_fresh_running_instance(
                    now=now,
                    timeout_seconds=self.heartbeat_timeout_seconds,
                    role="worker",
                )
            heartbeat = ServiceCheck(
                name="service_heartbeat",
                ok=is_fresh,
                detail=(
                    "fresh running service instance found"
                    if is_fresh
                    else "no fresh running service instance"
                ),
            )
            worker_heartbeat = ServiceCheck(
                name="worker_heartbeat",
                ok=worker_is_fresh,
                detail=(
                    "fresh running worker instance found"
                    if worker_is_fresh
                    else "no fresh running worker instance"
                ),
            )
        except Exception as exc:
            heartbeat = self._failed_check("service_heartbeat", exc)
            worker_heartbeat = self._failed_check("worker_heartbeat", exc)
        return ServiceHealthReport((*doctor.checks, heartbeat, worker_heartbeat))

    async def status(
        self,
        *,
        now: datetime,
        instance_limit: int = 20,
    ) -> ServiceStatusSnapshot:
        async with self.session_factory() as session:
            instance_rows = await ServiceInstanceRepository(session).list_recent(
                limit=instance_limit
            )
            pending = await self._task_count(session, TaskStatus.PENDING)
            running = await self._task_count(session, TaskStatus.RUNNING)
            failed = await self._task_count(session, TaskStatus.FAILED)
            oldest_pending_at = await session.scalar(
                select(func.min(CollectionTask.created_at)).where(
                    CollectionTask.status == TaskStatus.PENDING
                )
            )
            active_backoffs = int(
                await session.scalar(
                    select(func.count(RequestBackoffState.id)).where(
                        RequestBackoffState.fail_count > 0,
                        RequestBackoffState.backoff_until > now,
                    )
                )
                or 0
            )
            request_failures = await self._request_failure_window(session, now=now)
            alert_rows = list(
                await session.scalars(
                    select(OperationalAlertState)
                    .where(OperationalAlertState.status == "active")
                    .order_by(
                        OperationalAlertState.severity.asc(),
                        OperationalAlertState.last_triggered_at.desc(),
                        OperationalAlertState.alert_key.asc(),
                    )
                    .limit(instance_limit)
                )
            )

        instances = tuple(
            ServiceInstanceSummary(
                instance_id=row.instance_id,
                hostname=row.hostname,
                pid=row.pid,
                version=row.version,
                roles=tuple(row.roles),
                status=row.status,
                started_at=row.started_at,
                heartbeat_at=row.heartbeat_at,
                stopped_at=row.stopped_at,
                last_error_type=row.last_error_type,
                last_error_message=row.last_error_message,
            )
            for row in instance_rows
        )
        return ServiceStatusSnapshot(
            instances=instances,
            pending_tasks=pending,
            running_tasks=running,
            failed_tasks=failed,
            oldest_pending_at=_as_utc(oldest_pending_at),
            active_backoffs=active_backoffs,
            request_failures=request_failures,
            active_alerts=tuple(
                OperationalAlertSummary(
                    alert_key=row.alert_key,
                    alert_type=row.alert_type,
                    severity=row.severity,
                    summary=row.summary,
                    first_triggered_at=_as_utc(row.first_triggered_at),
                    last_triggered_at=_as_utc(row.last_triggered_at),
                    occurrence_count=row.occurrence_count,
                )
                for row in alert_rows
            ),
        )

    async def _database_check(self) -> ServiceCheck:
        try:
            async with self.session_factory() as session:
                await session.scalar(select(ServiceInstance.instance_id).limit(1))
            return ServiceCheck(
                name="database",
                ok=True,
                detail="database reachable and service schema available",
            )
        except Exception as exc:
            return self._failed_check("database", exc)

    async def _schema_revision_check(self) -> ServiceCheck:
        try:
            async with self.session_factory() as session:
                current = await get_current_schema_revision(session)
            expected = self.expected_schema_revision
            if current is None:
                return ServiceCheck(
                    name="schema_revision",
                    ok=False,
                    detail=f"schema revision missing; expected {expected}",
                )
            if current != expected:
                return ServiceCheck(
                    name="schema_revision",
                    ok=False,
                    detail=f"schema revision {current}; expected {expected}",
                )
            return ServiceCheck(
                name="schema_revision",
                ok=True,
                detail=f"schema revision {current}",
            )
        except Exception as exc:
            return self._failed_check("schema_revision", exc)

    def _storage_check(self, name: str, path: Path) -> ServiceCheck:
        probe_path: Path | None = None
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe_path = path / f".books-of-time-health-{uuid4().hex}"
            probe_path.write_bytes(b"ok")
            return ServiceCheck(name=name, ok=True, detail=f"writable: {path}")
        except Exception as exc:
            return self._failed_check(name, exc)
        finally:
            if probe_path is not None:
                probe_path.unlink(missing_ok=True)

    async def _raw_storage_check(self) -> ServiceCheck:
        try:
            return ServiceCheck(
                name="raw_storage",
                ok=True,
                detail=await asyncio.to_thread(self.raw_store.probe),
            )
        except Exception as exc:
            return self._failed_check("raw_storage", exc)

    async def _task_count(
        self,
        session: AsyncSession,
        status: TaskStatus,
    ) -> int:
        return int(
            await session.scalar(
                select(func.count(CollectionTask.id)).where(
                    CollectionTask.status == status
                )
            )
            or 0
        )

    async def _request_failure_window(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> RequestFailureWindow:
        since = now - timedelta(seconds=self.request_failure_window_seconds)
        row = (
            await session.execute(
                select(
                    func.count(CollectionCoverageStat.id),
                    func.sum(CollectionCoverageStat.pages_requested),
                    func.sum(CollectionCoverageStat.request_errors),
                    func.sum(CollectionCoverageStat.parse_errors),
                ).where(
                    CollectionCoverageStat.finished_at >= since,
                    CollectionCoverageStat.finished_at <= now,
                )
            )
        ).one()
        return RequestFailureWindow(
            since_at=since,
            until_at=now,
            coverage_runs=int(row[0] or 0),
            pages_requested=int(row[1] or 0),
            request_errors=int(row[2] or 0),
            parse_errors=int(row[3] or 0),
        )

    def _failed_check(self, name: str, exc: Exception) -> ServiceCheck:
        detail = f"{type(exc).__name__}: {exc}"
        return ServiceCheck(name=name, ok=False, detail=detail[:500])


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
