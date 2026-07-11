from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from books_of_time.coverage import CoverageDraft, EventCoverageSummary
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionRun,
    CollectionTask,
    CommentEntity,
    CommentObservation,
    CommentStateEvent,
    CommentVisibilityEvent,
    Event,
    EventKeyword,
    EventTarget,
    EventVideo,
    FrontierState,
    ImportantCommentWatchlist,
    RawPageObservation,
    RawPayload,
    RequestBackoffState,
    ScheduledJob,
    ServiceInstance,
    VideoAvailabilitySnapshot,
    VideoInfoSnapshot,
    VideoMetricSnapshot,
)
from books_of_time.domain.enums import (
    BilibiliRequestType,
    ScheduledJobKind,
    TaskKind,
    TaskStatus,
)
from books_of_time.domain.events import (
    EventTimelineRow,
    normalize_event_slug,
    normalize_event_target,
    validate_event_status,
    validate_event_window,
)
from books_of_time.domain.watchlist import (
    WatchlistPolicy,
    calculate_watchlist_priority,
)
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import RequestFailure
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedComment,
    ParsedCommentPage,
)
from books_of_time.parsers.video import (
    ParsedVideoAvailabilitySnapshot,
    ParsedVideoInfoSnapshot,
    ParsedVideoStats,
)
from books_of_time.storage.base import StoredRawPayload


class CollectionTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        *,
        kind: TaskKind,
        target_type: str,
        target_id: str,
        priority: int,
        payload: dict[str, Any],
        not_before: datetime,
        budget_cost: int = 1,
        max_retries: int = 3,
        idempotency_key: str | None = None,
    ) -> CollectionTask:
        if idempotency_key is not None:
            existing = await self.session.scalar(
                select(CollectionTask)
                .where(
                    CollectionTask.idempotency_key == idempotency_key,
                    CollectionTask.status.in_(
                        (
                            TaskStatus.PENDING,
                            TaskStatus.RUNNING,
                            TaskStatus.BACKOFF,
                        )
                    ),
                )
                .order_by(CollectionTask.created_at.asc(), CollectionTask.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if existing is not None:
                return existing

        task = CollectionTask(
            kind=kind,
            target_type=target_type,
            target_id=target_id,
            idempotency_key=idempotency_key,
            priority=priority,
            budget_cost=budget_cost,
            payload=payload,
            not_before=not_before,
            max_retries=max_retries,
            status=TaskStatus.PENDING,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def lease_next(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> CollectionTask | None:
        stmt = (
            select(CollectionTask)
            .where(
                CollectionTask.status == TaskStatus.PENDING,
                CollectionTask.not_before <= now,
            )
            .order_by(CollectionTask.priority.desc(), CollectionTask.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task = await self.session.scalar(stmt)
        if task is None:
            return None

        task.status = TaskStatus.RUNNING
        task.lease_owner = lease_owner
        task.lease_until = now + timedelta(seconds=lease_seconds)
        await self.session.flush()
        return task

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 20,
    ) -> list[CollectionTask]:
        stmt = select(CollectionTask).order_by(
            CollectionTask.priority.desc(),
            CollectionTask.created_at.asc(),
            CollectionTask.id.asc(),
        )
        if status is not None:
            stmt = stmt.where(CollectionTask.status == status)
        rows = await self.session.scalars(stmt.limit(limit))
        return list(rows)

    async def retry_failed(
        self,
        *,
        now: datetime,
        target_id: str | None = None,
        kind: TaskKind | None = None,
        limit: int = 100,
    ) -> int:
        stmt = (
            select(CollectionTask)
            .where(CollectionTask.status == TaskStatus.FAILED)
            .order_by(CollectionTask.updated_at.asc(), CollectionTask.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if target_id is not None:
            stmt = stmt.where(CollectionTask.target_id == target_id)
        if kind is not None:
            stmt = stmt.where(CollectionTask.kind == kind)

        tasks = list(await self.session.scalars(stmt))
        for task in tasks:
            task.status = TaskStatus.PENDING
            task.not_before = now
            task.lease_owner = None
            task.lease_until = None
            task.retry_count = 0

        await self.session.flush()
        return len(tasks)

    async def recover_expired_leases(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> int:
        rows = await self.session.scalars(
            select(CollectionTask)
            .where(
                CollectionTask.status == TaskStatus.RUNNING,
                CollectionTask.lease_until.is_not(None),
                CollectionTask.lease_until <= now,
            )
            .order_by(CollectionTask.lease_until.asc(), CollectionTask.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        tasks = list(rows)
        for task in tasks:
            task.status = TaskStatus.PENDING
            task.not_before = now
            task.lease_owner = None
            task.lease_until = None

        await self.session.flush()
        return len(tasks)


class CollectionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_running(
        self,
        *,
        run_id: str,
        worker_id: str,
        now: datetime,
    ) -> CollectionRun:
        run = await self.session.scalar(
            select(CollectionRun).where(CollectionRun.run_id == run_id)
        )
        if run is not None:
            return run

        run = CollectionRun(
            run_id=run_id,
            worker_id=worker_id,
            started_at=now,
            finished_at=None,
            status="running",
            tasks_started=0,
            tasks_succeeded=0,
            tasks_failed=0,
            extra={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def record_task_started(self, run: CollectionRun, *, now: datetime) -> None:
        run.tasks_started += 1
        run.status = "running"
        run.finished_at = None
        run.updated_at = now
        await self.session.flush()

    async def record_task_succeeded(
        self,
        run: CollectionRun,
        *,
        now: datetime,
    ) -> None:
        run.tasks_succeeded += 1
        run.status = "succeeded"
        run.finished_at = now
        run.updated_at = now
        await self.session.flush()

    async def record_task_failed(self, run: CollectionRun, *, now: datetime) -> None:
        run.tasks_failed += 1
        run.status = "failed"
        run.finished_at = now
        run.updated_at = now
        await self.session.flush()


class CollectionCoverageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_draft(
        self,
        *,
        task: CollectionTask,
        run_id: str,
        draft: CoverageDraft,
        started_at: datetime,
        finished_at: datetime,
    ) -> CollectionCoverageStat:
        stat = CollectionCoverageStat(
            collection_task_id=task.id,
            run_id=run_id,
            task_kind=draft.task_kind,
            target_type=draft.target_type,
            target_id=draft.target_id,
            started_at=started_at,
            finished_at=finished_at,
            status=draft.status,
            pages_requested=draft.pages_requested,
            pages_succeeded=draft.pages_succeeded,
            items_observed=draft.items_observed,
            raw_payloads_saved=draft.raw_payloads_saved,
            parse_errors=draft.parse_errors,
            request_errors=draft.request_errors,
            frontier_reached=draft.frontier_reached,
            frontier_missing=draft.frontier_missing,
            truncated=draft.truncated,
            corrupted=draft.corrupted,
            reason=draft.reason,
            extra=draft.extra,
            created_at=finished_at,
            updated_at=finished_at,
        )
        self.session.add(stat)
        await self.session.flush()
        return stat

    async def insert_failed(
        self,
        *,
        task: CollectionTask,
        run_id: str,
        started_at: datetime,
        finished_at: datetime,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> CollectionCoverageStat:
        draft = CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            request_errors=1,
            reason=reason,
            extra=extra or {},
        )
        stat = await self.insert_from_draft(
            task=task,
            run_id=run_id,
            draft=draft,
            started_at=started_at,
            finished_at=finished_at,
        )
        stat.status = "failed"
        await self.session.flush()
        return stat

    async def list_for_target(
        self,
        *,
        target_type: str,
        target_id: str,
        limit: int = 20,
    ) -> list[CollectionCoverageStat]:
        rows = await self.session.scalars(
            select(CollectionCoverageStat)
            .where(
                CollectionCoverageStat.target_type == target_type,
                CollectionCoverageStat.target_id == target_id,
            )
            .order_by(CollectionCoverageStat.finished_at.desc())
            .limit(limit)
        )
        return list(rows)


class RequestBackoffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_failure(
        self,
        *,
        platform: str,
        scope: str,
        failure: RequestFailure,
        now: datetime,
        default_seconds: Mapping[str, int],
        max_seconds: int,
    ) -> RequestBackoffState:
        state = await self.session.scalar(
            select(RequestBackoffState).where(
                RequestBackoffState.platform == platform,
                RequestBackoffState.request_type == failure.request_type,
                RequestBackoffState.scope == scope,
            )
        )
        if state is None:
            state = RequestBackoffState(
                platform=platform,
                request_type=failure.request_type,
                scope=scope,
                error_kind=failure.kind.value,
                status_code=failure.status_code,
                retry_after_seconds=failure.retry_after_seconds,
                fail_count=0,
                first_failed_at=now,
                last_failed_at=now,
                backoff_until=now,
                last_message=str(failure),
                extra={},
                created_at=now,
                updated_at=now,
            )
            self.session.add(state)

        state.fail_count += 1
        state.error_kind = failure.kind.value
        state.status_code = failure.status_code
        state.retry_after_seconds = failure.retry_after_seconds
        state.last_failed_at = now
        state.last_message = str(failure)
        state.backoff_until = now + timedelta(
            seconds=_backoff_seconds(
                failure=failure,
                fail_count=state.fail_count,
                default_seconds=default_seconds,
                max_seconds=max_seconds,
            )
        )
        state.updated_at = now
        await self.session.flush()
        return state

    async def reset_success(
        self,
        *,
        platform: str,
        scope: str,
        request_type: BilibiliRequestType,
        now: datetime,
    ) -> None:
        state = await self.session.scalar(
            select(RequestBackoffState).where(
                RequestBackoffState.platform == platform,
                RequestBackoffState.request_type == request_type,
                RequestBackoffState.scope == scope,
            )
        )
        if state is None:
            return
        state.fail_count = 0
        state.backoff_until = now
        state.updated_at = now
        await self.session.flush()

    async def reset_all_success(
        self,
        *,
        platform: str,
        scope: str,
        now: datetime,
    ) -> None:
        rows = await self.session.scalars(
            select(RequestBackoffState).where(
                RequestBackoffState.platform == platform,
                RequestBackoffState.scope == scope,
            )
        )
        for state in rows:
            state.fail_count = 0
            state.backoff_until = now
            state.updated_at = now
        await self.session.flush()


class ServiceInstanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(
        self,
        *,
        instance_id: str,
        hostname: str,
        pid: int,
        version: str,
        roles: list[str],
        now: datetime,
    ) -> ServiceInstance:
        instance = ServiceInstance(
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
            version=version,
            roles=list(roles),
            status="starting",
            started_at=now,
            heartbeat_at=now,
            stopped_at=None,
            last_error_type=None,
            last_error_message=None,
        )
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def get(self, instance_id: str) -> ServiceInstance | None:
        return await self.session.get(ServiceInstance, instance_id)

    async def list_recent(self, *, limit: int = 20) -> list[ServiceInstance]:
        return list(
            await self.session.scalars(
                select(ServiceInstance)
                .order_by(ServiceInstance.started_at.desc())
                .limit(min(max(limit, 1), 200))
            )
        )

    async def mark_running(
        self,
        instance_id: str,
        *,
        now: datetime,
    ) -> ServiceInstance:
        return await self._transition(instance_id, status="running", now=now)

    async def heartbeat(
        self,
        instance_id: str,
        *,
        now: datetime,
    ) -> ServiceInstance:
        instance = await self._get_required(instance_id)
        instance.heartbeat_at = now
        await self.session.flush()
        return instance

    async def mark_stopping(
        self,
        instance_id: str,
        *,
        now: datetime,
    ) -> ServiceInstance:
        return await self._transition(instance_id, status="stopping", now=now)

    async def mark_stopped(
        self,
        instance_id: str,
        *,
        now: datetime,
    ) -> ServiceInstance:
        instance = await self._transition(instance_id, status="stopped", now=now)
        instance.stopped_at = now
        await self.session.flush()
        return instance

    async def mark_failed(
        self,
        instance_id: str,
        *,
        now: datetime,
        error_type: str,
        error_message: str,
    ) -> ServiceInstance:
        instance = await self._transition(instance_id, status="failed", now=now)
        instance.stopped_at = now
        instance.last_error_type = error_type[:120]
        instance.last_error_message = error_message[:2000]
        await self.session.flush()
        return instance

    async def has_fresh_running_instance(
        self,
        *,
        now: datetime,
        timeout_seconds: float,
        role: str | None = None,
    ) -> bool:
        cutoff = now - timedelta(seconds=max(timeout_seconds, 0))
        instances = await self.session.scalars(
            select(ServiceInstance)
            .where(
                ServiceInstance.status == "running",
                ServiceInstance.heartbeat_at >= cutoff,
            )
            .order_by(ServiceInstance.heartbeat_at.desc())
        )
        if role is None:
            return next(iter(instances), None) is not None
        return any(role in instance.roles for instance in instances)

    async def _transition(
        self,
        instance_id: str,
        *,
        status: str,
        now: datetime,
    ) -> ServiceInstance:
        instance = await self._get_required(instance_id)
        instance.status = status
        instance.heartbeat_at = now
        await self.session.flush()
        return instance

    async def _get_required(self, instance_id: str) -> ServiceInstance:
        instance = await self.get(instance_id)
        if instance is None:
            raise LookupError(f"Unknown service instance: {instance_id}")
        return instance


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_event(
        self,
        *,
        slug: str,
        name: str,
        now: datetime,
        game: str | None = None,
        description: str | None = None,
        status: str = "active",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> Event:
        normalized_slug = normalize_event_slug(slug)
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Event name cannot be empty")
        validate_event_window(start_at, end_at)
        validate_event_status(status)
        if await self.session.scalar(
            select(Event.id).where(Event.slug == normalized_slug)
        ):
            raise ValueError(f"Event slug already exists: {normalized_slug}")

        event = Event(
            slug=normalized_slug,
            name=normalized_name,
            game=game.strip() if game and game.strip() else None,
            description=description.strip()
            if description and description.strip()
            else None,
            status=status,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone.strip() or "Asia/Shanghai",
            created_at=now,
            updated_at=now,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def resolve_event(self, reference: int | str) -> Event:
        event: Event | None
        if isinstance(reference, int) or str(reference).isdecimal():
            event = await self.session.get(Event, int(reference))
        else:
            slug = normalize_event_slug(str(reference))
            event = await self.session.scalar(select(Event).where(Event.slug == slug))
        if event is None:
            raise LookupError(f"Event not found: {reference}")
        return event

    async def list_events(self, *, limit: int = 100) -> list[Event]:
        _validate_limit(limit)
        rows = await self.session.scalars(
            select(Event)
            .order_by(Event.start_at.desc(), Event.created_at.desc(), Event.id.desc())
            .limit(limit)
        )
        return list(rows)

    async def add_target(
        self,
        *,
        event_id: int | str,
        target_type: str,
        target_value: str,
        now: datetime,
        priority: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> EventTarget:
        event = await self.resolve_event(event_id)
        normalized_value = normalize_event_target(target_type, target_value)
        display_value = (
            normalized_value
            if target_type in {"uid", "seed_bvid"}
            else " ".join(target_value.strip().split())
        )
        target = await self.session.scalar(
            select(EventTarget).where(
                EventTarget.event_id == event.id,
                EventTarget.target_type == target_type,
                EventTarget.normalized_value == normalized_value,
            )
        )
        created = target is None
        if target is None:
            target = EventTarget(
                event_id=event.id,
                target_type=target_type,
                target_value=display_value,
                normalized_value=normalized_value,
                priority=priority,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                extra=dict(extra or {}),
                created_at=now,
                updated_at=now,
            )
            self.session.add(target)
            await self.session.flush()
        else:
            target.target_value = display_value
            target.priority = max(target.priority, priority)
            target.active = True
            target.last_seen_at = now
            target.extra.update(extra or {})
            flag_modified(target, "extra")
            target.updated_at = now
            await self.session.flush()

        if target_type == "keyword":
            await self._synchronize_keyword(target=target, now=now)
        elif target_type == "seed_bvid":
            await self.attach_video(
                event_id=event.id,
                bvid=normalized_value,
                source_target_id=target.id,
                association_reason="seed_bvid",
                confidence=1.0,
                now=now,
            )
            if created:
                await CollectionTaskRepository(self.session).enqueue(
                    kind=TaskKind.FETCH_VIDEO_STATS,
                    target_type="video",
                    target_id=normalized_value,
                    priority=priority,
                    payload={
                        "bvid": normalized_value,
                        "event_id": event.id,
                        "source_target_id": target.id,
                        "reason": "event_seed",
                    },
                    not_before=now,
                    idempotency_key=(
                        f"{TaskKind.FETCH_VIDEO_STATS.value}:video:"
                        f"{normalized_value}:event:{event.id}"
                    ),
                )
        return target

    async def attach_video(
        self,
        *,
        event_id: int | str,
        bvid: str,
        association_reason: str,
        now: datetime,
        source_target_id: int | None = None,
        confidence: float = 1.0,
    ) -> EventVideo:
        if not 0 <= confidence <= 1:
            raise ValueError("Video association confidence must be between 0 and 1")
        reason = association_reason.strip()
        if not reason:
            raise ValueError("Video association reason cannot be empty")
        event = await self.resolve_event(event_id)
        normalized_bvid = normalize_event_target("seed_bvid", bvid)
        video = await self.session.get(EventVideo, (event.id, normalized_bvid))
        if video is None:
            video = EventVideo(
                event_id=event.id,
                bvid=normalized_bvid,
                source_target_id=source_target_id,
                association_reason=reason,
                confidence=confidence,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            self.session.add(video)
        else:
            video.source_target_id = source_target_id or video.source_target_id
            video.association_reason = reason
            video.confidence = confidence
            video.active = True
            video.last_seen_at = now
            video.updated_at = now
        await self.session.flush()
        return video

    async def list_videos(
        self,
        event_id: int | str,
        *,
        active_only: bool = True,
        limit: int = 1000,
    ) -> list[EventVideo]:
        _validate_limit(limit)
        event = await self.resolve_event(event_id)
        stmt = select(EventVideo).where(EventVideo.event_id == event.id)
        if active_only:
            stmt = stmt.where(EventVideo.active.is_(True))
        rows = await self.session.scalars(
            stmt.order_by(EventVideo.first_seen_at.desc(), EventVideo.bvid.asc()).limit(
                limit
            )
        )
        return list(rows)

    async def list_active_uid_targets(
        self,
        *,
        now: datetime,
    ) -> list[EventTarget]:
        rows = await self.session.scalars(
            select(EventTarget)
            .join(Event, Event.id == EventTarget.event_id)
            .where(
                EventTarget.target_type == "uid",
                EventTarget.active.is_(True),
                Event.status == "active",
                or_(Event.start_at.is_(None), Event.start_at <= now),
                or_(Event.end_at.is_(None), Event.end_at >= now),
            )
            .order_by(EventTarget.normalized_value.asc(), EventTarget.id.asc())
        )
        return list(rows)

    async def get_coverage_summary(
        self,
        reference: int | str,
    ) -> EventCoverageSummary:
        event = await self.resolve_event(reference)
        active_bvids = select(EventVideo.bvid).where(
            EventVideo.event_id == event.id,
            EventVideo.active.is_(True),
        )
        active_video_count = int(
            await self.session.scalar(
                select(func.count()).select_from(active_bvids.subquery())
            )
            or 0
        )
        row = (
            await self.session.execute(
                select(
                    func.count(CollectionCoverageStat.id).label("row_count"),
                    func.count(func.distinct(CollectionCoverageStat.target_id)).label(
                        "video_count"
                    ),
                    func.sum(
                        case((CollectionCoverageStat.status == "succeeded", 1), else_=0)
                    ).label("succeeded_count"),
                    func.sum(
                        case((CollectionCoverageStat.status == "partial", 1), else_=0)
                    ).label("partial_count"),
                    func.sum(
                        case((CollectionCoverageStat.status == "failed", 1), else_=0)
                    ).label("failed_count"),
                    func.sum(CollectionCoverageStat.pages_requested).label(
                        "pages_requested"
                    ),
                    func.sum(CollectionCoverageStat.pages_succeeded).label(
                        "pages_succeeded"
                    ),
                    func.sum(CollectionCoverageStat.items_observed).label(
                        "items_observed"
                    ),
                    func.sum(CollectionCoverageStat.raw_payloads_saved).label(
                        "raw_payloads_saved"
                    ),
                    func.sum(CollectionCoverageStat.parse_errors).label("parse_errors"),
                    func.sum(CollectionCoverageStat.request_errors).label(
                        "request_errors"
                    ),
                    func.sum(
                        case(
                            (CollectionCoverageStat.truncated.is_(True), 1),
                            else_=0,
                        )
                    ).label("truncated_count"),
                    func.sum(
                        case(
                            (CollectionCoverageStat.corrupted.is_(True), 1),
                            else_=0,
                        )
                    ).label("corrupted_count"),
                    func.min(CollectionCoverageStat.started_at).label(
                        "first_started_at"
                    ),
                    func.max(CollectionCoverageStat.finished_at).label(
                        "last_finished_at"
                    ),
                ).where(
                    CollectionCoverageStat.target_type == "video",
                    CollectionCoverageStat.target_id.in_(active_bvids),
                )
            )
        ).one()
        return EventCoverageSummary(
            event_id=event.id,
            event_slug=event.slug,
            active_video_count=active_video_count,
            videos_with_coverage=int(row.video_count or 0),
            coverage_row_count=int(row.row_count or 0),
            succeeded_count=int(row.succeeded_count or 0),
            partial_count=int(row.partial_count or 0),
            failed_count=int(row.failed_count or 0),
            pages_requested=int(row.pages_requested or 0),
            pages_succeeded=int(row.pages_succeeded or 0),
            items_observed=int(row.items_observed or 0),
            raw_payloads_saved=int(row.raw_payloads_saved or 0),
            parse_errors=int(row.parse_errors or 0),
            request_errors=int(row.request_errors or 0),
            truncated_count=int(row.truncated_count or 0),
            corrupted_count=int(row.corrupted_count or 0),
            first_started_at=row.first_started_at,
            last_finished_at=row.last_finished_at,
        )

    async def build_timeline(
        self,
        reference: int | str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_records: int | None = None,
    ) -> list[EventTimelineRow]:
        if since is not None and until is not None and until <= since:
            raise ValueError("until must be after since")
        if max_records is not None and not 1 <= max_records <= 2_000_000:
            raise ValueError("max_records must be between 1 and 2000000")
        event = await self.resolve_event(reference)
        event_videos = list(
            await self.session.scalars(
                select(EventVideo)
                .where(EventVideo.event_id == event.id)
                .order_by(EventVideo.bvid.asc())
            )
        )
        if not event_videos:
            return []

        bvids = [video.bvid for video in event_videos]
        metric_stmt = select(VideoMetricSnapshot).where(
            VideoMetricSnapshot.bvid.in_(bvids)
        )
        state_stmt = select(CommentStateEvent).where(CommentStateEvent.bvid.in_(bvids))
        visibility_stmt = select(CommentVisibilityEvent).where(
            CommentVisibilityEvent.bvid.in_(bvids)
        )
        if since is not None:
            metric_stmt = metric_stmt.where(VideoMetricSnapshot.captured_at >= since)
            state_stmt = state_stmt.where(CommentStateEvent.created_at >= since)
            visibility_stmt = visibility_stmt.where(
                CommentVisibilityEvent.created_at >= since
            )
        if until is not None:
            metric_stmt = metric_stmt.where(VideoMetricSnapshot.captured_at < until)
            state_stmt = state_stmt.where(CommentStateEvent.created_at < until)
            visibility_stmt = visibility_stmt.where(
                CommentVisibilityEvent.created_at < until
            )
        if max_records is not None:
            query_limit = max_records + 1
            metric_stmt = metric_stmt.limit(query_limit)
            state_stmt = state_stmt.limit(query_limit)
            visibility_stmt = visibility_stmt.limit(query_limit)
        metrics = list(await self.session.scalars(metric_stmt))
        state_events = list(await self.session.scalars(state_stmt))
        visibility_events = list(await self.session.scalars(visibility_stmt))

        rows = [
            EventTimelineRow(
                event_id=event.id,
                event_slug=event.slug,
                timestamp=video.first_seen_at,
                record_type="event_video_associated",
                source_table="event_videos",
                source_key=f"event_videos:{event.id}:{video.bvid}",
                bvid=video.bvid,
                data={
                    "active": video.active,
                    "association_reason": video.association_reason,
                    "confidence": video.confidence,
                    "source_target_id": video.source_target_id,
                },
            )
            for video in event_videos
            if (since is None or video.first_seen_at >= since)
            and (until is None or video.first_seen_at < until)
        ]
        rows.extend(
            EventTimelineRow(
                event_id=event.id,
                event_slug=event.slug,
                timestamp=metric.captured_at,
                record_type="video_metric_snapshot",
                source_table="video_metric_snapshots",
                source_key=(
                    f"video_metric_snapshots:{metric.bvid}:"
                    f"{metric.captured_at.isoformat()}"
                ),
                bvid=metric.bvid,
                data={
                    "view_count": metric.view_count,
                    "like_count": metric.like_count,
                    "coin_count": metric.coin_count,
                    "favorite_count": metric.favorite_count,
                    "share_count": metric.share_count,
                    "reply_count": metric.reply_count,
                    "danmaku_count": metric.danmaku_count,
                    "raw_payload_id": metric.raw_payload_id,
                },
            )
            for metric in metrics
        )
        rows.extend(
            EventTimelineRow(
                event_id=event.id,
                event_slug=event.slug,
                timestamp=state_event.created_at,
                record_type="comment_state_event",
                source_table="comment_state_events",
                source_key=f"comment_state_events:{state_event.id}",
                bvid=state_event.bvid,
                data={
                    "rpid": state_event.rpid,
                    "event_type": state_event.event_type,
                    "previous_comment_observation_id": (
                        state_event.previous_comment_observation_id
                    ),
                    "current_comment_observation_id": (
                        state_event.current_comment_observation_id
                    ),
                    "old_value": state_event.old_value,
                    "new_value": state_event.new_value,
                },
            )
            for state_event in state_events
        )
        rows.extend(
            EventTimelineRow(
                event_id=event.id,
                event_slug=event.slug,
                timestamp=visibility_event.created_at,
                record_type="comment_visibility_event",
                source_table="comment_visibility_events",
                source_key=f"comment_visibility_events:{visibility_event.id}",
                bvid=visibility_event.bvid,
                data={
                    "rpid": visibility_event.rpid,
                    "event_type": visibility_event.event_type,
                    "previous_comment_observation_id": (
                        visibility_event.previous_comment_observation_id
                    ),
                    "current_comment_observation_id": (
                        visibility_event.current_comment_observation_id
                    ),
                    "old_visibility": visibility_event.old_visibility,
                    "new_visibility": visibility_event.new_visibility,
                    "missing_reason": visibility_event.missing_reason,
                },
            )
            for visibility_event in visibility_events
        )
        rows.sort(key=lambda row: (row.timestamp, row.record_type, row.source_key))
        if max_records is not None and len(rows) > max_records:
            raise ValueError(
                f"Event timeline exceeds max_records={max_records}; narrow the window"
            )
        return rows

    async def resolve_active_uid_target(
        self,
        *,
        event_id: int,
        target_id: int,
        now: datetime,
    ) -> EventTarget | None:
        return await self.session.scalar(
            select(EventTarget)
            .join(Event, Event.id == EventTarget.event_id)
            .where(
                EventTarget.id == target_id,
                EventTarget.event_id == event_id,
                EventTarget.target_type == "uid",
                EventTarget.active.is_(True),
                Event.status == "active",
                or_(Event.start_at.is_(None), Event.start_at <= now),
                or_(Event.end_at.is_(None), Event.end_at >= now),
            )
        )

    async def _synchronize_keyword(
        self,
        *,
        target: EventTarget,
        now: datetime,
    ) -> EventKeyword:
        keyword = await self.session.scalar(
            select(EventKeyword).where(
                EventKeyword.event_id == target.event_id,
                EventKeyword.normalized_keyword == target.normalized_value,
                EventKeyword.version == 1,
            )
        )
        if keyword is None:
            keyword = EventKeyword(
                event_id=target.event_id,
                keyword=target.target_value,
                normalized_keyword=target.normalized_value,
                category="topic",
                version=1,
                active=True,
                source_target_id=target.id,
                created_at=now,
                updated_at=now,
            )
            self.session.add(keyword)
        else:
            keyword.keyword = target.target_value
            keyword.active = True
            keyword.source_target_id = target.id
            keyword.updated_at = now
        await self.session.flush()
        return keyword


class ScheduledJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure(
        self,
        *,
        job_key: str,
        job_kind: ScheduledJobKind,
        schedule_seconds: int,
        priority: int,
        payload: dict[str, Any],
        next_run_at: datetime,
        enabled: bool = True,
    ) -> ScheduledJob:
        job = await self.session.scalar(
            select(ScheduledJob).where(ScheduledJob.job_key == job_key)
        )
        if job is None:
            job = ScheduledJob(
                job_key=job_key,
                job_kind=job_kind,
                schedule_seconds=max(int(schedule_seconds), 1),
                priority=int(priority),
                payload=dict(payload),
                enabled=enabled,
                next_run_at=next_run_at,
                lease_owner=None,
                lease_until=None,
                last_started_at=None,
                last_succeeded_at=None,
                last_failed_at=None,
                consecutive_failures=0,
                last_error_type=None,
                last_error_message=None,
            )
            self.session.add(job)
        else:
            job.job_kind = job_kind
            job.schedule_seconds = max(int(schedule_seconds), 1)
            job.priority = int(priority)
            job.payload = dict(payload)
            job.enabled = enabled
            flag_modified(job, "payload")
        await self.session.flush()
        return job

    async def lease_due(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> ScheduledJob | None:
        job = await self.session.scalar(
            select(ScheduledJob)
            .where(
                ScheduledJob.enabled.is_(True),
                ScheduledJob.next_run_at <= now,
                or_(
                    ScheduledJob.lease_owner.is_(None),
                    ScheduledJob.lease_until.is_(None),
                    ScheduledJob.lease_until <= now,
                ),
            )
            .order_by(
                ScheduledJob.priority.desc(),
                ScheduledJob.next_run_at.asc(),
                ScheduledJob.id.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.lease_owner = lease_owner
        job.lease_until = now + timedelta(seconds=max(int(lease_seconds), 1))
        job.last_started_at = now
        await self.session.flush()
        return job

    async def mark_succeeded(
        self,
        job: ScheduledJob,
        *,
        now: datetime,
    ) -> ScheduledJob:
        interval_seconds = max(int(job.schedule_seconds), 1)
        next_run_at = job.next_run_at + timedelta(seconds=interval_seconds)
        if next_run_at <= now:
            missed_slots = (
                int((now - next_run_at).total_seconds() // interval_seconds) + 1
            )
            next_run_at += timedelta(seconds=interval_seconds * missed_slots)
        job.next_run_at = next_run_at
        job.lease_owner = None
        job.lease_until = None
        job.last_succeeded_at = now
        job.consecutive_failures = 0
        job.last_error_type = None
        job.last_error_message = None
        await self.session.flush()
        return job

    async def mark_failed(
        self,
        job: ScheduledJob,
        *,
        now: datetime,
        retry_delay_seconds: int,
        error: BaseException,
    ) -> ScheduledJob:
        job.next_run_at = now + timedelta(seconds=max(int(retry_delay_seconds), 1))
        job.lease_owner = None
        job.lease_until = None
        job.last_failed_at = now
        job.consecutive_failures += 1
        job.last_error_type = type(error).__name__[:120]
        job.last_error_message = str(error)[:2000]
        await self.session.flush()
        return job


class VideoMetricSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_bvid(
        self,
        *,
        bvid: str,
        limit: int = 20,
    ) -> list[VideoMetricSnapshot]:
        rows = await self.session.scalars(
            select(VideoMetricSnapshot)
            .where(VideoMetricSnapshot.bvid == bvid)
            .order_by(VideoMetricSnapshot.captured_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def get_view_growth_since(
        self,
        *,
        bvid: str,
        since: datetime,
        now: datetime,
    ) -> int | None:
        latest = await self.session.scalar(
            select(VideoMetricSnapshot)
            .where(
                VideoMetricSnapshot.bvid == bvid,
                VideoMetricSnapshot.captured_at <= now,
                VideoMetricSnapshot.view_count.is_not(None),
            )
            .order_by(VideoMetricSnapshot.captured_at.desc())
            .limit(1)
        )
        if latest is None:
            return None

        baseline = await self.session.scalar(
            select(VideoMetricSnapshot)
            .where(
                VideoMetricSnapshot.bvid == bvid,
                VideoMetricSnapshot.captured_at <= since,
                VideoMetricSnapshot.view_count.is_not(None),
            )
            .order_by(VideoMetricSnapshot.captured_at.desc())
            .limit(1)
        )
        if baseline is None:
            baseline = await self.session.scalar(
                select(VideoMetricSnapshot)
                .where(
                    VideoMetricSnapshot.bvid == bvid,
                    VideoMetricSnapshot.captured_at > since,
                    VideoMetricSnapshot.captured_at <= now,
                    VideoMetricSnapshot.view_count.is_not(None),
                )
                .order_by(VideoMetricSnapshot.captured_at.asc())
                .limit(1)
            )
        if baseline is None or baseline.captured_at == latest.captured_at:
            return None
        return max(int(latest.view_count) - int(baseline.view_count), 0)

    async def insert_from_parsed(
        self,
        parsed: ParsedVideoStats,
    ) -> VideoMetricSnapshot:
        snapshot = VideoMetricSnapshot(
            bvid=parsed.bvid,
            captured_at=parsed.captured_at,
            view_count=parsed.view_count,
            like_count=parsed.like_count,
            coin_count=parsed.coin_count,
            favorite_count=parsed.favorite_count,
            share_count=parsed.share_count,
            reply_count=parsed.reply_count,
            danmaku_count=parsed.danmaku_count,
            raw_payload_id=parsed.raw_payload_id,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot


class VideoInfoSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_parsed(
        self,
        parsed: ParsedVideoInfoSnapshot,
    ) -> VideoInfoSnapshot:
        snapshot = VideoInfoSnapshot(
            bvid=parsed.bvid,
            captured_at=parsed.captured_at,
            title=parsed.title,
            description=parsed.description,
            owner_mid=parsed.owner_mid,
            owner_name=parsed.owner_name,
            tags=parsed.tags,
            raw_payload_id=parsed.raw_payload_id,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot


class VideoAvailabilitySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_parsed(
        self,
        parsed: ParsedVideoAvailabilitySnapshot,
    ) -> VideoAvailabilitySnapshot:
        snapshot = VideoAvailabilitySnapshot(
            bvid=parsed.bvid,
            captured_at=parsed.captured_at,
            status=parsed.status,
            bili_code=parsed.bili_code,
            bili_message=parsed.bili_message,
            http_status_code=parsed.http_status_code,
            raw_payload_id=parsed.raw_payload_id,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot


class RawPayloadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, raw_payload_id: int) -> RawPayload | None:
        return await self.session.get(RawPayload, raw_payload_id)

    async def insert_from_fetch_result(
        self,
        *,
        result: FetchResult,
        stored: StoredRawPayload,
        parser_version: str | None = None,
    ) -> RawPayload:
        raw = RawPayload(
            captured_at=result.captured_at,
            request_type=result.request_type,
            method=result.method,
            url_hash=hashlib.sha256(result.url.encode()).digest(),
            params_hash=_hash_params(result.params),
            status_code=result.status_code,
            payload_hash=bytes.fromhex(stored.payload_hash_hex),
            storage_uri=stored.storage_uri,
            compressed_size=stored.compressed_size,
            uncompressed_size=stored.uncompressed_size,
            parser_version=parser_version,
            created_at=result.captured_at,
        )
        self.session.add(raw)
        await self.session.flush()
        return raw


class RawPageObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_parsed_page(
        self,
        parsed: ParsedCommentPage,
        *,
        request_type: BilibiliRequestType,
    ) -> RawPageObservation:
        observation = RawPageObservation(
            raw_payload_id=parsed.raw_payload_id,
            captured_at=parsed.captured_at,
            request_type=request_type,
            target_type="video",
            target_id=parsed.bvid,
            page_number=parsed.page_number,
            cursor=parsed.extra.get("request_offset"),
            sort_mode=parsed.sort_mode,
            parser_version=COMMENT_PARSER_VERSION,
            status="success",
            item_count=len(parsed.comments),
            extra=parsed.extra,
        )
        self.session.add(observation)
        await self.session.flush()
        return observation


class CommentRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        watchlist_policy: WatchlistPolicy | None = None,
    ) -> None:
        self.session = session
        self.watchlist_policy = watchlist_policy or WatchlistPolicy()

    async def upsert_page(
        self,
        parsed: ParsedCommentPage,
        *,
        raw_page_observation_id: int,
    ) -> list[CommentObservation]:
        observations: list[CommentObservation] = []
        for comment in parsed.comments:
            _entity, is_new_entity = await self._ensure_entity(
                comment,
                captured_at=parsed.captured_at,
                raw_payload_id=parsed.raw_payload_id,
            )
            previous_observation = None
            if not is_new_entity:
                previous_observation = await self._latest_observation(comment.rpid)

            observation = CommentObservation(
                rpid=comment.rpid,
                bvid=comment.bvid,
                oid=comment.oid,
                captured_at=parsed.captured_at,
                raw_payload_id=parsed.raw_payload_id,
                raw_page_observation_id=raw_page_observation_id,
                sort_mode=parsed.sort_mode,
                page_number=parsed.page_number,
                position=comment.position,
                content=comment.content,
                content_hash=comment.content_hash,
                like_count=comment.like_count,
                reply_count=comment.reply_count,
                author_mid=comment.author_mid,
                author_name=comment.author_name,
                is_deleted=False,
                visibility="visible",
                extra={},
            )
            self.session.add(observation)
            await self.session.flush()
            if is_new_entity or previous_observation is None:
                self._add_state_event(
                    rpid=comment.rpid,
                    bvid=comment.bvid,
                    previous_observation_id=None,
                    current_observation_id=observation.id,
                    event_type="first_seen",
                    old_value={},
                    new_value={"rpid": comment.rpid, "bvid": comment.bvid},
                    created_at=parsed.captured_at,
                )
            else:
                self._add_changed_state_events(
                    previous=previous_observation,
                    current=observation,
                    created_at=parsed.captured_at,
                )
            await self._add_reappeared_event_if_needed(
                current=observation,
                created_at=parsed.captured_at,
            )
            await self._upsert_watchlist_candidates(
                previous=previous_observation,
                current=observation,
                is_first_seen=is_new_entity,
                is_root=comment.root_rpid in (None, 0),
                created_at=parsed.captured_at,
            )
            observations.append(observation)
        await self.session.flush()
        return observations

    async def mark_disappeared(
        self,
        *,
        rpid: int,
        bvid: str,
        missing_reason: str,
        created_at: datetime,
    ) -> CommentVisibilityEvent | None:
        latest_event = await self._latest_visibility_event(rpid)
        if latest_event is not None and latest_event.event_type == "disappeared":
            return None

        previous = await self._latest_observation(rpid)
        event = CommentVisibilityEvent(
            rpid=rpid,
            bvid=bvid,
            previous_comment_observation_id=previous.id if previous else None,
            current_comment_observation_id=None,
            event_type="disappeared",
            old_visibility=previous.visibility if previous else "visible",
            new_visibility="missing",
            missing_reason=missing_reason,
            created_at=created_at,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def _ensure_entity(
        self,
        comment: ParsedComment,
        *,
        captured_at: datetime,
        raw_payload_id: int,
    ) -> tuple[CommentEntity, bool]:
        entity = await self.session.get(CommentEntity, comment.rpid)
        if entity is not None:
            entity.updated_at = captured_at
            return entity, False

        entity = CommentEntity(
            rpid=comment.rpid,
            oid=comment.oid,
            bvid=comment.bvid,
            root_rpid=comment.root_rpid,
            parent_rpid=comment.parent_rpid,
            author_mid=comment.author_mid,
            author_name=comment.author_name,
            first_content=comment.content,
            first_content_hash=comment.content_hash,
            first_seen_at=captured_at,
            first_raw_payload_id=raw_payload_id,
            created_at=captured_at,
            updated_at=captured_at,
        )
        self.session.add(entity)
        await self.session.flush()
        return entity, True

    async def _latest_observation(self, rpid: int) -> CommentObservation | None:
        return await self.session.scalar(
            select(CommentObservation)
            .where(CommentObservation.rpid == rpid)
            .order_by(
                CommentObservation.captured_at.desc(), CommentObservation.id.desc()
            )
            .limit(1)
        )

    async def _latest_visibility_event(
        self,
        rpid: int,
    ) -> CommentVisibilityEvent | None:
        return await self.session.scalar(
            select(CommentVisibilityEvent)
            .where(CommentVisibilityEvent.rpid == rpid)
            .order_by(
                CommentVisibilityEvent.created_at.desc(),
                CommentVisibilityEvent.id.desc(),
            )
            .limit(1)
        )

    async def _add_reappeared_event_if_needed(
        self,
        *,
        current: CommentObservation,
        created_at: datetime,
    ) -> None:
        latest_event = await self._latest_visibility_event(current.rpid)
        if latest_event is None or latest_event.event_type != "disappeared":
            return

        self.session.add(
            CommentVisibilityEvent(
                rpid=current.rpid,
                bvid=current.bvid,
                previous_comment_observation_id=(
                    latest_event.previous_comment_observation_id
                ),
                current_comment_observation_id=current.id,
                event_type="reappeared",
                old_visibility="missing",
                new_visibility=current.visibility,
                missing_reason=latest_event.missing_reason,
                created_at=created_at,
            )
        )

    async def _upsert_watchlist_candidates(
        self,
        *,
        previous: CommentObservation | None,
        current: CommentObservation,
        is_first_seen: bool,
        is_root: bool,
        created_at: datetime,
    ) -> None:
        if not is_root:
            return
        candidate = calculate_watchlist_priority(
            policy=self.watchlist_policy,
            content=current.content,
            sort_mode=current.sort_mode,
            position=current.position,
            previous_reply_count=(previous.reply_count if previous else None),
            current_reply_count=current.reply_count,
            previous_like_count=(previous.like_count if previous else None),
            current_like_count=current.like_count,
            is_first_seen=is_first_seen,
        )
        if candidate is None:
            return
        await self._upsert_watchlist_item(
            current=current,
            reason=candidate.reason,
            priority=candidate.priority,
            score=candidate.score,
            extra=candidate.extra,
            created_at=created_at,
        )

    async def _upsert_watchlist_item(
        self,
        *,
        current: CommentObservation,
        reason: str,
        priority: int,
        score: float,
        extra: dict[str, Any],
        created_at: datetime,
    ) -> None:
        item = await self.session.scalar(
            select(ImportantCommentWatchlist)
            .where(
                ImportantCommentWatchlist.bvid == current.bvid,
                ImportantCommentWatchlist.rpid == current.rpid,
            )
            .limit(1)
        )
        if item is None:
            item = ImportantCommentWatchlist(
                bvid=current.bvid,
                rpid=current.rpid,
                root_rpid=current.rpid,
                reason=reason,
                priority=priority,
                score=score,
                reply_count=current.reply_count,
                like_count=current.like_count,
                hot_position=current.position if current.sort_mode == "hot" else None,
                last_comment_observation_id=current.id,
                first_seen_at=created_at,
                last_seen_at=created_at,
                expires_at=created_at + timedelta(days=1),
                active=True,
                extra=extra,
                created_at=created_at,
                updated_at=created_at,
            )
            self.session.add(item)
        elif priority >= item.priority:
            item.reason = reason
            item.priority = priority
            item.score = score
            item.extra = extra
        item.reply_count = current.reply_count
        item.like_count = current.like_count
        if current.sort_mode == "hot":
            item.hot_position = current.position
        item.last_comment_observation_id = current.id
        item.last_seen_at = created_at
        item.expires_at = created_at + timedelta(days=1)
        item.active = True
        item.updated_at = created_at
        await CollectionTaskRepository(self.session).enqueue(
            kind=TaskKind.FETCH_COMMENT_REPLIES,
            target_type="comment",
            target_id=str(current.rpid),
            priority=priority,
            payload={
                "bvid": current.bvid,
                "aid": current.oid,
                "root_rpid": current.rpid,
                "page": 1,
                "page_limit": 1,
                "page_size": 20,
                "reason": reason,
            },
            not_before=created_at,
            idempotency_key=(
                f"{TaskKind.FETCH_COMMENT_REPLIES.value}:"
                f"comment:{current.bvid}:{current.rpid}:watchlist"
            ),
        )

    def _add_changed_state_events(
        self,
        *,
        previous: CommentObservation,
        current: CommentObservation,
        created_at: datetime,
    ) -> None:
        if previous.content_hash != current.content_hash:
            self._add_state_event(
                rpid=current.rpid,
                bvid=current.bvid,
                previous_observation_id=previous.id,
                current_observation_id=current.id,
                event_type="content_hash_changed",
                old_value={"content_hash": previous.content_hash.hex()},
                new_value={"content_hash": current.content_hash.hex()},
                created_at=created_at,
            )

        previous_like_bucket = _like_bucket(previous.like_count)
        current_like_bucket = _like_bucket(current.like_count)
        if previous_like_bucket != current_like_bucket:
            self._add_state_event(
                rpid=current.rpid,
                bvid=current.bvid,
                previous_observation_id=previous.id,
                current_observation_id=current.id,
                event_type="like_bucket_changed",
                old_value={
                    "bucket": previous_like_bucket,
                    "count": previous.like_count,
                },
                new_value={
                    "bucket": current_like_bucket,
                    "count": current.like_count,
                },
                created_at=created_at,
            )

        if previous.reply_count != current.reply_count:
            self._add_state_event(
                rpid=current.rpid,
                bvid=current.bvid,
                previous_observation_id=previous.id,
                current_observation_id=current.id,
                event_type="reply_count_changed",
                old_value={"reply_count": previous.reply_count},
                new_value={"reply_count": current.reply_count},
                created_at=created_at,
            )

        if (
            previous.sort_mode == "hot"
            and current.sort_mode == "hot"
            and previous.position != current.position
        ):
            self._add_state_event(
                rpid=current.rpid,
                bvid=current.bvid,
                previous_observation_id=previous.id,
                current_observation_id=current.id,
                event_type="hot_position_changed",
                old_value={
                    "page_number": previous.page_number,
                    "position": previous.position,
                },
                new_value={
                    "page_number": current.page_number,
                    "position": current.position,
                },
                created_at=created_at,
            )

    def _add_state_event(
        self,
        *,
        rpid: int,
        bvid: str,
        previous_observation_id: int | None,
        current_observation_id: int,
        event_type: str,
        old_value: dict[str, Any],
        new_value: dict[str, Any],
        created_at: datetime,
    ) -> None:
        self.session.add(
            CommentStateEvent(
                rpid=rpid,
                bvid=bvid,
                previous_comment_observation_id=previous_observation_id,
                current_comment_observation_id=current_observation_id,
                event_type=event_type,
                old_value=old_value,
                new_value=new_value,
                created_at=created_at,
            )
        )


class FrontierStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self,
        *,
        target_type: str,
        target_id: str,
        frontier_type: str,
        now: datetime,
    ) -> FrontierState:
        stmt = select(FrontierState).where(
            FrontierState.target_type == target_type,
            FrontierState.target_id == target_id,
            FrontierState.frontier_type == frontier_type,
        )
        state = await self.session.scalar(stmt)
        if state is not None:
            return state

        state = FrontierState(
            target_type=target_type,
            target_id=target_id,
            frontier_type=frontier_type,
            frontier_rpid=None,
            frontier_time=None,
            cursor=None,
            last_scan_at=None,
            last_scan_status=None,
            last_scan_pages=0,
            last_scan_truncated=False,
            extra={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(state)
        await self.session.flush()
        return state

    async def save(self, state: FrontierState) -> FrontierState:
        flag_modified(state, "extra")
        await self.session.flush()
        return state


def _hash_params(params: dict[str, Any] | None) -> bytes | None:
    if not params:
        return None
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(canonical).digest()


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")


def _like_bucket(count: int | None) -> str:
    if count is None:
        return "unknown"
    if count < 10:
        return "0-9"
    if count < 100:
        return "10-99"
    if count < 1000:
        return "100-999"
    if count < 10000:
        return "1k-9999"
    if count < 100000:
        return "10k-99999"
    return "100k+"


def _backoff_seconds(
    *,
    failure: RequestFailure,
    fail_count: int,
    default_seconds: Mapping[str, int],
    max_seconds: int,
) -> int:
    base = failure.retry_after_seconds
    if base is None:
        base = int(default_seconds.get(failure.kind.value, 300))
    multiplier = 2 ** min(max(fail_count - 1, 0), 5)
    return min(int(base) * multiplier, max_seconds)
