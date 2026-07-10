from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import ScheduledJob
from books_of_time.db.repositories import CollectionTaskRepository, EventRepository
from books_of_time.domain.enums import ScheduledJobKind, TaskKind
from books_of_time.service.coordinator import (
    ScheduledJobDefinition,
    ScheduledJobHandler,
)
from books_of_time.task_orchestrator.discovery_loop import DiscoveryUidSource
from books_of_time.task_orchestrator.discovery_sources import (
    resolve_discovery_uid_sources,
)
from books_of_time.task_orchestrator.video_snapshot_scheduler import (
    VideoSnapshotScheduler,
)


class UidDiscoveryScheduleHandler:
    def __init__(self, sources: list[DiscoveryUidSource]) -> None:
        self.sources = list(sources)

    async def handle(
        self,
        job: ScheduledJob,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> None:
        repo = CollectionTaskRepository(session)
        scheduled_for = job.next_run_at.isoformat()
        sources_by_mid: dict[str, DiscoveryUidSource] = {}
        for source in self.sources:
            sources_by_mid.setdefault(source.mid, source)
        event_links_by_mid: dict[str, list[dict[str, int]]] = {}
        event_targets = await EventRepository(session).list_active_uid_targets(now=now)
        for target in event_targets:
            sources_by_mid.setdefault(
                target.normalized_value,
                DiscoveryUidSource(mid=target.normalized_value, pool_type="event"),
            )
            event_links_by_mid.setdefault(target.normalized_value, []).append(
                {"event_id": target.event_id, "target_id": target.id}
            )

        for source in sources_by_mid.values():
            await repo.enqueue(
                kind=TaskKind.DISCOVER_USER_VIDEOS,
                target_type="user",
                target_id=source.mid,
                priority=110,
                payload={
                    "mid": source.mid,
                    "page": 1,
                    "source_pool_type": source.pool_type,
                    "source_pool_id": source.pool_id,
                    "reason": "scheduled_discovery",
                    "scheduled_for": scheduled_for,
                    "event_links": event_links_by_mid.get(source.mid, []),
                },
                not_before=now,
                idempotency_key=(
                    f"{TaskKind.DISCOVER_USER_VIDEOS.value}:user:{source.mid}:"
                    f"{scheduled_for}"
                ),
            )


class VideoSnapshotSweepScheduleHandler:
    def __init__(self, scheduler: VideoSnapshotScheduler | None = None) -> None:
        self.scheduler = scheduler or VideoSnapshotScheduler()

    async def handle(
        self,
        job: ScheduledJob,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> None:
        await self.scheduler.schedule_due_snapshots(session=session, now=now)


class TerminalSnapshotScheduleHandler:
    def __init__(self, scheduler: VideoSnapshotScheduler | None = None) -> None:
        self.scheduler = scheduler or VideoSnapshotScheduler()

    async def handle(
        self,
        job: ScheduledJob,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> None:
        await self.scheduler.schedule_terminal_snapshots(session=session, now=now)


def build_default_scheduled_jobs(
    cfg: dict,
) -> tuple[
    list[ScheduledJobDefinition],
    dict[ScheduledJobKind, ScheduledJobHandler],
]:
    scheduler_cfg = cfg.get("scheduler", {})
    sources = resolve_discovery_uid_sources(cfg.get("discovery", {}))
    definitions = [
        ScheduledJobDefinition(
            job_key="uid-discovery",
            job_kind=ScheduledJobKind.UID_DISCOVERY,
            schedule_seconds=max(
                int(scheduler_cfg.get("discovery_scan_seconds", 60)),
                1,
            ),
            priority=100,
            payload={},
        ),
        ScheduledJobDefinition(
            job_key="video-snapshot-sweep",
            job_kind=ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP,
            schedule_seconds=60,
            priority=90,
            payload={},
        ),
        ScheduledJobDefinition(
            job_key="daily-terminal-snapshot",
            job_kind=ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT,
            schedule_seconds=60,
            priority=95,
            payload={},
        ),
    ]
    handlers: dict[ScheduledJobKind, ScheduledJobHandler] = {
        ScheduledJobKind.UID_DISCOVERY: UidDiscoveryScheduleHandler(sources),
        ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP: VideoSnapshotSweepScheduleHandler(),
        ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT: (TerminalSnapshotScheduleHandler()),
    }
    return definitions, handlers
