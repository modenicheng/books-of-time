from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import CollectionTask
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionRunRepository,
    CollectionTaskRepository,
    RequestBackoffRepository,
)
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.http.errors import RequestFailure


class Collector(Protocol):
    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft: ...


class Worker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        collectors: Mapping[TaskKind, Collector],
        run_id: str,
        lease_owner: str,
        lease_seconds: int = 120,
        retry_delay_seconds: int = 300,
        request_backoff_defaults: Mapping[str, int] | None = None,
        request_backoff_max_seconds: int = 21600,
    ) -> None:
        self.session_factory = session_factory
        self.collectors = collectors
        self.run_id = run_id
        self.lease_owner = lease_owner
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.request_backoff_defaults = dict(
            request_backoff_defaults
            or {
                "timeout": 60,
                "403": 1800,
                "429": 900,
                "captcha": 3600,
                "5xx": 300,
                "parse_error": 300,
            }
        )
        self.request_backoff_max_seconds = request_backoff_max_seconds

    async def run_once(self, *, now: datetime | None = None) -> bool:
        effective_now = now or datetime.now(UTC)
        async with self.session_factory() as session:
            repo = CollectionTaskRepository(session)
            task = await repo.lease_next(
                lease_owner=self.lease_owner,
                now=effective_now,
                lease_seconds=self.lease_seconds,
            )
            if task is None:
                await session.rollback()
                return False

            run_repo = CollectionRunRepository(session)
            coverage_repo = CollectionCoverageRepository(session)
            run = await run_repo.get_or_create_running(
                run_id=self.run_id,
                worker_id=self.lease_owner,
                now=effective_now,
            )
            await run_repo.record_task_started(run, now=effective_now)

            collector = self.collectors[task.kind]
            try:
                draft = await collector.collect(task, session)
            except Exception as exc:
                finished_at = datetime.now(UTC)
                backoff_until = effective_now + timedelta(
                    seconds=self.retry_delay_seconds
                )
                if isinstance(exc, RequestFailure):
                    backoff = await RequestBackoffRepository(session).record_failure(
                        platform="bilibili",
                        scope="global",
                        failure=exc,
                        now=effective_now,
                        default_seconds=self.request_backoff_defaults,
                        max_seconds=self.request_backoff_max_seconds,
                    )
                    backoff_until = backoff.backoff_until
                    reason = exc.kind.value
                    extra = {
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "request_type": exc.request_type.value,
                        "status_code": exc.status_code,
                    }
                else:
                    reason = "collector_exception"
                    extra = {
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    }
                await coverage_repo.insert_failed(
                    task=task,
                    run_id=self.run_id,
                    started_at=effective_now,
                    finished_at=finished_at,
                    reason=reason,
                    extra=extra,
                )
                await run_repo.record_task_failed(run, now=finished_at)
                task.retry_count += 1
                if task.retry_count <= task.max_retries:
                    task.status = TaskStatus.PENDING
                    task.not_before = backoff_until
                else:
                    task.status = TaskStatus.FAILED
                task.lease_owner = None
                task.lease_until = None
                await session.commit()
                raise

            finished_at = datetime.now(UTC)
            await coverage_repo.insert_from_draft(
                task=task,
                run_id=self.run_id,
                draft=draft,
                started_at=effective_now,
                finished_at=finished_at,
            )
            await run_repo.record_task_succeeded(run, now=finished_at)
            task.status = TaskStatus.SUCCEEDED
            task.lease_owner = None
            task.lease_until = None
            await session.commit()
            return True
