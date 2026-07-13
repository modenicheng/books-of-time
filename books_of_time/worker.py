from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.coverage import CoverageDraft
from books_of_time.db.http_evidence import DatabaseHttpEvidenceSink
from books_of_time.db.models import CollectionTask
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionRunRepository,
    CollectionTaskRepository,
    RequestBackoffRepository,
)
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.http.errors import RequestFailure
from books_of_time.http.evidence import capture_http_evidence
from books_of_time.storage.base import RawPayloadStore


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
        raw_store: RawPayloadStore | None = None,
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
                "network": 60,
                "403": 1800,
                "429": 900,
                "captcha": 3600,
                "5xx": 300,
                "parse_error": 300,
            }
        )
        self.request_backoff_max_seconds = request_backoff_max_seconds
        self.raw_store = raw_store

    async def run_once(self, *, now: datetime | None = None) -> bool:
        effective_now = now or datetime.now(UTC)
        async with self.session_factory() as session:
            repo = CollectionTaskRepository(session)
            await repo.recover_expired_leases(now=effective_now)
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

            collector = self.collectors.get(task.kind)
            if collector is None:
                finished_at = datetime.now(UTC)
                await coverage_repo.insert_failed(
                    task=task,
                    run_id=self.run_id,
                    started_at=effective_now,
                    finished_at=finished_at,
                    reason="no_collector",
                    extra={"task_kind": task.kind.value},
                )
                await run_repo.record_task_failed(run, now=finished_at)
                task.status = TaskStatus.FAILED
                task.lease_owner = None
                task.lease_until = None
                await session.commit()
                return True
            evidence_sink = (
                DatabaseHttpEvidenceSink(
                    session=session,
                    raw_store=self.raw_store,
                    run_id=self.run_id,
                    collection_task_id=task.id,
                )
                if self.raw_store is not None
                else None
            )
            try:
                with capture_http_evidence(evidence_sink):
                    draft = await collector.collect(task, session)
            except Exception as exc:
                finished_at = datetime.now(UTC)
                if evidence_sink is not None:
                    await evidence_sink.mark_abandoned(
                        finished_at=finished_at,
                        error_message=str(exc),
                    )
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
                return True

            finished_at = datetime.now(UTC)
            await coverage_repo.insert_from_draft(
                task=task,
                run_id=self.run_id,
                draft=draft,
                started_at=effective_now,
                finished_at=finished_at,
            )
            await run_repo.record_task_succeeded(run, now=finished_at)
            await RequestBackoffRepository(session).reset_all_success(
                platform="bilibili",
                scope="global",
                now=finished_at,
            )
            task.status = TaskStatus.SUCCEEDED
            task.lease_owner = None
            task.lease_until = None
            await session.commit()
            return True

    async def run_loop(
        self,
        *,
        idle_sleep_seconds: float = 5,
        max_iterations: int | None = None,
        stop_when_idle: bool = False,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> int:
        sleep_func = sleep or asyncio.sleep
        iterations = 0
        executed_count = 0

        while (max_iterations is None or iterations < max_iterations) and not (
            stop_event is not None and stop_event.is_set()
        ):
            iterations += 1
            executed = await self.run_once()
            if executed:
                executed_count += 1
                continue

            if stop_when_idle:
                break
            maybe_awaitable = sleep_func(idle_sleep_seconds)
            if maybe_awaitable is not None:
                await maybe_awaitable

        return executed_count
