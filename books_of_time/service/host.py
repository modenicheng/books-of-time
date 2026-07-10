from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.repositories import ServiceInstanceRepository


class ServiceWorker(Protocol):
    async def run_loop(
        self,
        *,
        idle_sleep_seconds: float,
        max_iterations: int | None,
        stop_event: asyncio.Event,
    ) -> int: ...


class ServiceCoordinator(Protocol):
    async def run_loop(
        self,
        *,
        stop_event: asyncio.Event,
        max_iterations: int | None = None,
    ) -> int: ...


class ServiceHost:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        worker: ServiceWorker,
        coordinator: ServiceCoordinator | None = None,
        instance_id: str,
        roles: list[str],
        hostname: str,
        pid: int,
        version: str,
        heartbeat_seconds: float = 10,
        shutdown_grace_seconds: float = 60,
        worker_idle_sleep_seconds: float = 5,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.coordinator = coordinator
        self.instance_id = instance_id
        self.roles = list(roles)
        self.hostname = hostname
        self.pid = pid
        self.version = version
        self.heartbeat_seconds = max(heartbeat_seconds, 0.01)
        self.shutdown_grace_seconds = max(shutdown_grace_seconds, 0)
        self.worker_idle_sleep_seconds = max(worker_idle_sleep_seconds, 0)
        self.stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self, *, max_worker_iterations: int | None = None) -> int:
        await self._register()
        await self._mark_running()

        worker_task = asyncio.create_task(
            self.worker.run_loop(
                idle_sleep_seconds=self.worker_idle_sleep_seconds,
                max_iterations=max_worker_iterations,
                stop_event=self.stop_event,
            ),
            name=f"{self.instance_id}:worker",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"{self.instance_id}:heartbeat",
        )
        stop_task = asyncio.create_task(
            self.stop_event.wait(),
            name=f"{self.instance_id}:stop",
        )
        coordinator_task = (
            asyncio.create_task(
                self.coordinator.run_loop(stop_event=self.stop_event),
                name=f"{self.instance_id}:coordinator",
            )
            if self.coordinator is not None
            else None
        )
        tasks = tuple(
            task
            for task in (worker_task, heartbeat_task, stop_task, coordinator_task)
            if task is not None
        )

        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if worker_task in done:
                result = await worker_task
            elif stop_task in done:
                result = await self._finish_worker(worker_task)
            elif heartbeat_task in done:
                await heartbeat_task
                raise RuntimeError("Heartbeat loop stopped unexpectedly")
            else:
                assert coordinator_task is not None
                await coordinator_task
                raise RuntimeError("Coordinator loop stopped unexpectedly")

            await self._mark_stopping()
            self.stop_event.set()
            if not worker_task.done():
                result = await self._finish_worker(worker_task)
            await heartbeat_task
            if coordinator_task is not None:
                await coordinator_task
            await self._mark_stopped()
            return result
        except BaseException as exc:
            self.stop_event.set()
            await self._drain_tasks(tasks)
            await self._mark_failed(exc)
            raise
        finally:
            self.stop_event.set()
            await self._cancel_tasks(tasks)

    async def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._heartbeat()
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.heartbeat_seconds,
                )
            except TimeoutError:
                continue

    async def _finish_worker(self, worker_task: asyncio.Task[int]) -> int:
        try:
            return await asyncio.wait_for(
                asyncio.shield(worker_task),
                timeout=self.shutdown_grace_seconds,
            )
        except TimeoutError:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            return 0

    async def _register(self) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            await ServiceInstanceRepository(session).register(
                instance_id=self.instance_id,
                hostname=self.hostname,
                pid=self.pid,
                version=self.version,
                roles=self.roles,
                now=now,
            )
            await session.commit()

    async def _mark_running(self) -> None:
        async with self.session_factory() as session:
            await ServiceInstanceRepository(session).mark_running(
                self.instance_id,
                now=datetime.now(UTC),
            )
            await session.commit()

    async def _heartbeat(self) -> None:
        async with self.session_factory() as session:
            await ServiceInstanceRepository(session).heartbeat(
                self.instance_id,
                now=datetime.now(UTC),
            )
            await session.commit()

    async def _mark_stopping(self) -> None:
        async with self.session_factory() as session:
            await ServiceInstanceRepository(session).mark_stopping(
                self.instance_id,
                now=datetime.now(UTC),
            )
            await session.commit()

    async def _mark_stopped(self) -> None:
        async with self.session_factory() as session:
            await ServiceInstanceRepository(session).mark_stopped(
                self.instance_id,
                now=datetime.now(UTC),
            )
            await session.commit()

    async def _mark_failed(self, exc: BaseException) -> None:
        async with self.session_factory() as session:
            await ServiceInstanceRepository(session).mark_failed(
                self.instance_id,
                now=datetime.now(UTC),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            await session.commit()

    async def _cancel_tasks(self, tasks: tuple[asyncio.Task, ...]) -> None:
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _drain_tasks(self, tasks: tuple[asyncio.Task, ...]) -> None:
        current = asyncio.current_task()
        active = [task for task in tasks if task is not current and not task.done()]
        if not active:
            return
        drain_seconds = min(
            self.shutdown_grace_seconds,
            self.heartbeat_seconds + 1,
        )
        _, pending = await asyncio.wait(active, timeout=drain_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
