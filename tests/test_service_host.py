from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.repositories import ServiceInstanceRepository
from books_of_time.service.host import ServiceHost


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FiniteWorker:
    def __init__(self, result: int = 3) -> None:
        self.result = result
        self.stop_event: asyncio.Event | None = None
        self.max_iterations: int | None = None

    async def run_loop(
        self,
        *,
        idle_sleep_seconds: float,
        max_iterations: int | None,
        stop_event: asyncio.Event,
    ) -> int:
        self.stop_event = stop_event
        self.max_iterations = max_iterations
        return self.result


class WaitingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.saw_stop = False

    async def run_loop(
        self,
        *,
        idle_sleep_seconds: float,
        max_iterations: int | None,
        stop_event: asyncio.Event,
    ) -> int:
        self.started.set()
        await stop_event.wait()
        self.saw_stop = True
        return 0


class FailingWorker:
    async def run_loop(
        self,
        *,
        idle_sleep_seconds: float,
        max_iterations: int | None,
        stop_event: asyncio.Event,
    ) -> int:
        raise RuntimeError("worker exploded")


class HangingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_loop(
        self,
        *,
        idle_sleep_seconds: float,
        max_iterations: int | None,
        stop_event: asyncio.Event,
    ) -> int:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class WaitingCoordinator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_event: asyncio.Event | None = None
        self.saw_stop = False

    async def run_loop(
        self,
        *,
        stop_event: asyncio.Event,
        max_iterations: int | None = None,
    ) -> int:
        self.stop_event = stop_event
        self.started.set()
        await stop_event.wait()
        self.saw_stop = True
        return 0


class FailingCoordinator:
    async def run_loop(
        self,
        *,
        stop_event: asyncio.Event,
        max_iterations: int | None = None,
    ) -> int:
        raise RuntimeError("coordinator exploded")


def _host(
    session_factory,
    worker,
    instance_id: str,
    *,
    grace: float = 1,
    coordinator=None,
) -> ServiceHost:
    return ServiceHost(
        session_factory=session_factory,
        worker=worker,
        coordinator=coordinator,
        instance_id=instance_id,
        roles=["worker"],
        hostname="collector-host",
        pid=789,
        version="0.1.0",
        heartbeat_seconds=0.01,
        shutdown_grace_seconds=grace,
        worker_idle_sleep_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_service_host_records_normal_finite_lifecycle() -> None:
    engine, session_factory = await _session_factory()
    worker = FiniteWorker(result=3)
    host = _host(session_factory, worker, "service-finite")

    result = await host.run(max_worker_iterations=1)

    async with session_factory() as session:
        instance = await ServiceInstanceRepository(session).get("service-finite")
    assert result == 3
    assert worker.max_iterations == 1
    assert worker.stop_event is not None
    assert instance is not None
    assert instance.status == "stopped"
    assert instance.stopped_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_host_request_stop_reaches_worker() -> None:
    engine, session_factory = await _session_factory()
    worker = WaitingWorker()
    host = _host(session_factory, worker, "service-stop")
    running = asyncio.create_task(host.run())
    await worker.started.wait()

    host.request_stop()
    result = await running

    async with session_factory() as session:
        instance = await ServiceInstanceRepository(session).get("service-stop")
    assert result == 0
    assert worker.saw_stop is True
    assert instance is not None
    assert instance.status == "stopped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_host_records_worker_failure_and_reraises() -> None:
    engine, session_factory = await _session_factory()
    host = _host(session_factory, FailingWorker(), "service-failure")

    with pytest.raises(RuntimeError, match="worker exploded"):
        await host.run()

    async with session_factory() as session:
        instance = await ServiceInstanceRepository(session).get("service-failure")
    assert instance is not None
    assert instance.status == "failed"
    assert instance.last_error_type == "RuntimeError"
    assert instance.last_error_message == "worker exploded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_host_cancels_worker_after_shutdown_grace() -> None:
    engine, session_factory = await _session_factory()
    worker = HangingWorker()
    host = _host(session_factory, worker, "service-timeout", grace=0.01)
    running = asyncio.create_task(host.run())
    await worker.started.wait()

    host.request_stop()
    result = await running

    async with session_factory() as session:
        instance = await ServiceInstanceRepository(session).get("service-timeout")
    assert result == 0
    assert worker.cancelled.is_set()
    assert instance is not None
    assert instance.status == "stopped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_host_stops_coordinator_with_finite_worker() -> None:
    engine, session_factory = await _session_factory()
    worker = FiniteWorker(result=1)
    coordinator = WaitingCoordinator()
    host = _host(
        session_factory,
        worker,
        "service-coordinator-stop",
        coordinator=coordinator,
    )

    result = await host.run(max_worker_iterations=1)

    assert result == 1
    assert coordinator.saw_stop is True
    assert coordinator.stop_event is worker.stop_event
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_host_records_coordinator_failure_and_reraises() -> None:
    engine, session_factory = await _session_factory()
    host = _host(
        session_factory,
        WaitingWorker(),
        "service-coordinator-failure",
        coordinator=FailingCoordinator(),
    )

    with pytest.raises(RuntimeError, match="coordinator exploded"):
        await host.run()

    async with session_factory() as session:
        instance = await ServiceInstanceRepository(session).get(
            "service-coordinator-failure"
        )
    assert instance is not None
    assert instance.status == "failed"
    assert instance.last_error_type == "RuntimeError"
    assert instance.last_error_message == "coordinator exploded"
    await engine.dispose()
