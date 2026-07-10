from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.common.logger import get_logger
from books_of_time.db.models import ScheduledJob
from books_of_time.db.repositories import ScheduledJobRepository
from books_of_time.domain.enums import ScheduledJobKind

logger = get_logger(__name__)


@dataclass(frozen=True)
class ScheduledJobDefinition:
    job_key: str
    job_kind: ScheduledJobKind
    schedule_seconds: int
    priority: int
    payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


class ScheduledJobHandler(Protocol):
    async def handle(
        self,
        job: ScheduledJob,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> None: ...


class ScheduledJobCoordinator:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        definitions: list[ScheduledJobDefinition],
        handlers: Mapping[ScheduledJobKind, ScheduledJobHandler],
        lease_owner: str,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
        idle_sleep_seconds: float = 1,
    ) -> None:
        self.session_factory = session_factory
        self.definitions = list(definitions)
        self.handlers = dict(handlers)
        self.lease_owner = lease_owner
        self.lease_seconds = max(int(lease_seconds), 1)
        self.retry_delay_seconds = max(int(retry_delay_seconds), 1)
        self.idle_sleep_seconds = max(float(idle_sleep_seconds), 0)

    async def bootstrap(self, *, now: datetime) -> None:
        async with self.session_factory() as session:
            repo = ScheduledJobRepository(session)
            for definition in self.definitions:
                await repo.ensure(
                    job_key=definition.job_key,
                    job_kind=definition.job_kind,
                    schedule_seconds=definition.schedule_seconds,
                    priority=definition.priority,
                    payload=definition.payload,
                    next_run_at=now,
                    enabled=definition.enabled,
                )
            await session.commit()

    async def run_once(self, *, now: datetime | None = None) -> bool:
        effective_now = now or datetime.now(UTC)
        async with self.session_factory() as session:
            job = await ScheduledJobRepository(session).lease_due(
                lease_owner=self.lease_owner,
                now=effective_now,
                lease_seconds=self.lease_seconds,
            )
            if job is None:
                await session.rollback()
                return False
            job_id = job.id
            await session.commit()

        try:
            async with self.session_factory() as session:
                job = await session.get(ScheduledJob, job_id)
                if job is None:
                    raise LookupError(f"Scheduled job disappeared: {job_id}")
                handler = self.handlers.get(job.job_kind)
                if handler is None:
                    raise LookupError(
                        f"No scheduled job handler registered for {job.job_kind.value}"
                    )
                await handler.handle(job, session, now=effective_now)
                await ScheduledJobRepository(session).mark_succeeded(
                    job,
                    now=effective_now,
                )
                await session.commit()
        except Exception as exc:
            async with self.session_factory() as session:
                failed_job = await session.get(ScheduledJob, job_id)
                if failed_job is None:
                    raise LookupError(
                        f"Scheduled job disappeared after failure: {job_id}"
                    ) from exc
                await ScheduledJobRepository(session).mark_failed(
                    failed_job,
                    now=effective_now,
                    retry_delay_seconds=self.retry_delay_seconds,
                    error=exc,
                )
                await session.commit()
            logger.warning(
                "Scheduled job failed job_id=%s kind=%s error=%s",
                job_id,
                failed_job.job_kind.value,
                exc,
            )
        return True

    async def run_loop(
        self,
        *,
        stop_event: asyncio.Event,
        max_iterations: int | None = None,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
    ) -> int:
        if stop_event.is_set():
            return 0
        await self.bootstrap(now=datetime.now(UTC))
        sleep_func = sleep or asyncio.sleep
        iterations = 0
        executed_count = 0

        while (
            max_iterations is None or iterations < max_iterations
        ) and not stop_event.is_set():
            iterations += 1
            if await self.run_once():
                executed_count += 1
                continue
            maybe_awaitable = sleep_func(self.idle_sleep_seconds)
            if maybe_awaitable is not None:
                await maybe_awaitable

        return executed_count
