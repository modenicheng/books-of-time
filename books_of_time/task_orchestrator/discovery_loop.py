from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.common.logger import get_logger
from books_of_time.http.client import FetchResult
from books_of_time.parsers.discovery import parse_user_video_list
from books_of_time.task_orchestrator.discovery import DiscoveryScheduler
from books_of_time.task_orchestrator.video_snapshot_scheduler import (
    VideoSnapshotScheduler,
)

logger = get_logger(__name__)


class DiscoveryVideoClient(Protocol):
    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult: ...


@dataclass(frozen=True)
class DiscoveryLoopResult:
    uids_scanned: int = 0
    videos_seen: int = 0
    videos_created: int = 0
    errors: int = 0

    def add(self, other: DiscoveryLoopResult) -> DiscoveryLoopResult:
        return DiscoveryLoopResult(
            uids_scanned=self.uids_scanned + other.uids_scanned,
            videos_seen=self.videos_seen + other.videos_seen,
            videos_created=self.videos_created + other.videos_created,
            errors=self.errors + other.errors,
        )


@dataclass(frozen=True)
class DiscoveryUidSource:
    mid: str
    pool_type: str = "matrix"
    pool_id: str | None = None


class DiscoveryLoop:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: DiscoveryVideoClient,
        matrix_uids: list[str] | None = None,
        uid_sources: list[DiscoveryUidSource] | None = None,
        fresh_video_window: timedelta = timedelta(minutes=2),
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        if uid_sources is not None:
            self.uid_sources = [
                DiscoveryUidSource(
                    mid=str(source.mid),
                    pool_type=source.pool_type,
                    pool_id=source.pool_id,
                )
                for source in uid_sources
            ]
        else:
            self.uid_sources = [
                DiscoveryUidSource(mid=str(uid)) for uid in (matrix_uids or [])
            ]
        self.scheduler = DiscoveryScheduler(
            session_factory=session_factory,
            fresh_video_window=fresh_video_window,
        )
        self.snapshot_scheduler = VideoSnapshotScheduler()

    async def run_once(self, *, now: datetime | None = None) -> DiscoveryLoopResult:
        effective_now = now or datetime.now(UTC)
        result = DiscoveryLoopResult()

        for source in self.uid_sources:
            try:
                fetched = await self.client.get_user_video_list(mid=source.mid, page=1)
                videos = parse_user_video_list(
                    json.loads(fetched.body),
                    source_mid=source.mid,
                    source_pool_type=source.pool_type,
                    source_pool_id=source.pool_id,
                )
                async with self.session_factory() as session:
                    created = await self.scheduler.handle_discovered_videos(
                        session=session,
                        videos=videos,
                        now=effective_now,
                    )
                    await self.snapshot_scheduler.schedule_terminal_snapshots(
                        session=session,
                        now=effective_now,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning(
                    "Discovery scan failed for uid=%s pool=%s:%s: %s",
                    source.mid,
                    source.pool_type,
                    source.pool_id,
                    exc,
                )
                result = result.add(DiscoveryLoopResult(errors=1))
                continue

            result = result.add(
                DiscoveryLoopResult(
                    uids_scanned=1,
                    videos_seen=len(videos),
                    videos_created=len(created),
                )
            )

        return result

    async def run_loop(
        self,
        *,
        interval_seconds: float,
        max_iterations: int | None = None,
        stop_when_idle: bool = False,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
    ) -> DiscoveryLoopResult:
        sleep_func = sleep or asyncio.sleep
        iterations = 0
        aggregate = DiscoveryLoopResult()

        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            result = await self.run_once()
            aggregate = aggregate.add(result)

            if stop_when_idle and result.videos_created == 0:
                break
            if max_iterations is not None and iterations >= max_iterations:
                break

            maybe_awaitable = sleep_func(interval_seconds)
            if maybe_awaitable is not None:
                await maybe_awaitable

        return aggregate
