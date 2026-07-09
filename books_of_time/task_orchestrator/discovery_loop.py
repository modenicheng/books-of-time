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


class DiscoveryLoop:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: DiscoveryVideoClient,
        matrix_uids: list[str],
        fresh_video_window: timedelta = timedelta(minutes=2),
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.matrix_uids = [str(uid) for uid in matrix_uids]
        self.scheduler = DiscoveryScheduler(
            session_factory=session_factory,
            fresh_video_window=fresh_video_window,
        )

    async def run_once(self, *, now: datetime | None = None) -> DiscoveryLoopResult:
        effective_now = now or datetime.now(UTC)
        result = DiscoveryLoopResult()

        for mid in self.matrix_uids:
            try:
                fetched = await self.client.get_user_video_list(mid=mid, page=1)
                videos = parse_user_video_list(
                    json.loads(fetched.body),
                    source_mid=mid,
                )
                async with self.session_factory() as session:
                    created = await self.scheduler.handle_discovered_videos(
                        session=session,
                        videos=videos,
                        now=effective_now,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("Discovery scan failed for uid=%s: %s", mid, exc)
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
