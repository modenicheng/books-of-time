from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionRun,
    CollectionTask,
    CommentEntity,
    CommentObservation,
    CommentStateEvent,
    CommentVisibilityEvent,
    FrontierState,
    ImportantCommentWatchlist,
    RawPageObservation,
    RawPayload,
    RequestBackoffState,
    ServiceInstance,
    VideoAvailabilitySnapshot,
    VideoInfoSnapshot,
    VideoMetricSnapshot,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
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
from books_of_time.storage.filesystem import StoredRawPayload


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
    ) -> bool:
        cutoff = now - timedelta(seconds=max(timeout_seconds, 0))
        instance_id = await self.session.scalar(
            select(ServiceInstance.instance_id)
            .where(
                ServiceInstance.status == "running",
                ServiceInstance.heartbeat_at >= cutoff,
            )
            .order_by(ServiceInstance.heartbeat_at.desc())
            .limit(1)
        )
        return instance_id is not None

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
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
        created_at: datetime,
    ) -> None:
        if (
            current.sort_mode == "hot"
            and current.position is not None
            and current.position <= 3
        ):
            await self._upsert_watchlist_item(
                current=current,
                reason="hot_top",
                priority=100 - current.position,
                score=float(100 - current.position),
                extra={"hot_position": current.position},
                created_at=created_at,
            )

        if (
            previous is not None
            and previous.reply_count is not None
            and current.reply_count is not None
        ):
            reply_delta = current.reply_count - previous.reply_count
            if reply_delta >= 5:
                priority = 80 + min(reply_delta, 19)
                await self._upsert_watchlist_item(
                    current=current,
                    reason="reply_growth",
                    priority=priority,
                    score=float(reply_delta),
                    extra={"reply_delta": reply_delta},
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
