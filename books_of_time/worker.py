from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import CollectionTask
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus


class Collector(Protocol):
    async def collect(self, task: CollectionTask, session: AsyncSession) -> None: ...


class Worker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        collectors: Mapping[TaskKind, Collector],
        lease_owner: str,
        lease_seconds: int = 120,
        retry_delay_seconds: int = 300,
    ) -> None:
        self.session_factory = session_factory
        self.collectors = collectors
        self.lease_owner = lease_owner
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds

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

            collector = self.collectors[task.kind]
            try:
                await collector.collect(task, session)
            except Exception:
                task.retry_count += 1
                if task.retry_count <= task.max_retries:
                    task.status = TaskStatus.PENDING
                    task.not_before = effective_now + timedelta(
                        seconds=self.retry_delay_seconds
                    )
                else:
                    task.status = TaskStatus.FAILED
                task.lease_owner = None
                task.lease_until = None
                await session.commit()
                raise

            task.status = TaskStatus.SUCCEEDED
            task.lease_owner = None
            task.lease_until = None
            await session.commit()
            return True
