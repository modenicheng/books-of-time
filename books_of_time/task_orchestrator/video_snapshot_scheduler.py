from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionTask,
    KnownVideo,
    VideoAvailabilitySnapshot,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind
from books_of_time.task_orchestrator.snapshot_policy import CoreWindow
from books_of_time.task_orchestrator.video_snapshot_policy import (
    get_next_video_snapshot_at,
)


class VideoSnapshotScheduler:
    async def schedule_due_snapshots(
        self,
        *,
        session: AsyncSession,
        now: datetime,
        limit: int = 500,
    ) -> list[CollectionTask]:
        videos = list(
            await session.scalars(
                select(KnownVideo)
                .order_by(KnownVideo.pubdate.desc(), KnownVideo.bvid.asc())
                .limit(min(max(limit, 1), 5000))
            )
        )
        repo = CollectionTaskRepository(session)
        tasks: list[CollectionTask] = []
        window = CoreWindow()
        for video in videos:
            if not await self._is_video_available(session, video.bvid):
                continue
            latest_at = await session.scalar(
                select(VideoMetricSnapshot.captured_at)
                .where(VideoMetricSnapshot.bvid == video.bvid)
                .order_by(VideoMetricSnapshot.captured_at.desc())
                .limit(1)
            )
            if latest_at is None:
                due_at = now
            else:
                due_at = await get_next_video_snapshot_at(
                    session,
                    bvid=video.bvid,
                    published_at=video.pubdate,
                    now=latest_at,
                )
                if due_at is None and window.allows_detail_polling(now):
                    due_at = now
            if due_at is None or due_at > now:
                continue
            task = await repo.enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id=video.bvid,
                priority=85,
                payload={
                    "bvid": video.bvid,
                    "reason": "snapshot_sweep",
                    "due_at": due_at.isoformat(),
                },
                not_before=now,
                idempotency_key=(
                    f"{TaskKind.FETCH_VIDEO_STATS.value}:video:{video.bvid}:"
                    f"sweep:{due_at.isoformat()}"
                ),
            )
            tasks.append(task)
        return tasks

    async def schedule_next_for_video(
        self,
        *,
        session: AsyncSession,
        bvid: str,
        now: datetime,
    ) -> CollectionTask | None:
        known = await session.scalar(select(KnownVideo).where(KnownVideo.bvid == bvid))
        if known is None:
            return None

        if not await self._is_video_available(session, bvid):
            return None

        next_at = await get_next_video_snapshot_at(
            session,
            bvid=bvid,
            published_at=known.pubdate,
            now=now,
        )
        if next_at is None:
            return None

        return await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id=bvid,
            priority=80,
            payload={
                "bvid": bvid,
                "reason": "snapshot_policy",
            },
            not_before=next_at,
            idempotency_key=(
                f"{TaskKind.FETCH_VIDEO_STATS.value}:video:{bvid}:"
                f"snapshot:{next_at.isoformat()}"
            ),
        )

    async def schedule_terminal_snapshots(
        self,
        *,
        session: AsyncSession,
        now: datetime,
        core_window: CoreWindow | None = None,
    ) -> list[CollectionTask]:
        window = core_window or CoreWindow()
        terminal_at, terminal_date = _terminal_at_for_day(now, window)
        if now < terminal_at:
            return []

        videos = list(
            await session.scalars(
                select(KnownVideo)
                .where(
                    KnownVideo.pubdate <= terminal_at,
                    KnownVideo.first_seen_at <= terminal_at,
                )
                .order_by(KnownVideo.bvid.asc())
            )
        )
        repo = CollectionTaskRepository(session)
        tasks: list[CollectionTask] = []
        for video in videos:
            if not await self._is_video_available(session, video.bvid):
                continue
            task = await repo.enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id=video.bvid,
                priority=95,
                payload={
                    "bvid": video.bvid,
                    "reason": "daily_terminal_snapshot",
                    "terminal_date": terminal_date,
                },
                not_before=terminal_at,
                idempotency_key=(
                    f"{TaskKind.FETCH_VIDEO_STATS.value}:video:{video.bvid}:"
                    f"terminal:{terminal_date}"
                ),
            )
            tasks.append(task)
        return tasks

    async def _is_video_available(self, session: AsyncSession, bvid: str) -> bool:
        latest = await session.scalar(
            select(VideoAvailabilitySnapshot)
            .where(VideoAvailabilitySnapshot.bvid == bvid)
            .order_by(VideoAvailabilitySnapshot.captured_at.desc())
            .limit(1)
        )
        if latest is None:
            return True
        return latest.status == "visible"


def _terminal_at_for_day(now: datetime, window: CoreWindow) -> tuple[datetime, str]:
    timezone = ZoneInfo(window.timezone_name)
    local_now = now.astimezone(timezone)
    terminal_local = local_now.replace(
        hour=window.stop_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    return terminal_local.astimezone(now.tzinfo), terminal_local.date().isoformat()
