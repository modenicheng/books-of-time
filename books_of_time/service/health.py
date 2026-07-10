from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import (
    CollectionTask,
    RequestBackoffState,
    ServiceInstance,
)
from books_of_time.db.repositories import ServiceInstanceRepository
from books_of_time.domain.enums import TaskStatus
from books_of_time.service.models import (
    ServiceCheck,
    ServiceHealthReport,
    ServiceInstanceSummary,
    ServiceStatusSnapshot,
)


class ServiceHealthChecker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        raw_dir: str | Path,
        media_dir: str | Path,
        heartbeat_timeout_seconds: float = 30,
    ) -> None:
        self.session_factory = session_factory
        self.raw_dir = Path(raw_dir)
        self.media_dir = Path(media_dir)
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds

    async def doctor(self) -> ServiceHealthReport:
        checks = [await self._database_check()]
        checks.append(self._storage_check("raw_storage", self.raw_dir))
        checks.append(self._storage_check("media_storage", self.media_dir))
        return ServiceHealthReport(tuple(checks))

    async def health(self, *, now: datetime) -> ServiceHealthReport:
        doctor = await self.doctor()
        try:
            async with self.session_factory() as session:
                is_fresh = await ServiceInstanceRepository(
                    session
                ).has_fresh_running_instance(
                    now=now,
                    timeout_seconds=self.heartbeat_timeout_seconds,
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
        except Exception as exc:
            heartbeat = self._failed_check("service_heartbeat", exc)
        return ServiceHealthReport((*doctor.checks, heartbeat))

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

    def _failed_check(self, name: str, exc: Exception) -> ServiceCheck:
        detail = f"{type(exc).__name__}: {exc}"
        return ServiceCheck(name=name, ok=False, detail=detail[:500])


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
