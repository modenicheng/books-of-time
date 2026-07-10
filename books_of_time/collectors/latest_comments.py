from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import (
    CollectionTask,
    CommentObservation,
    FrontierState,
    RawPageObservation,
    RawPayload,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    CommentRepository,
    FrontierStateRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.media.normalizer import MediaService
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedCommentPage,
    parse_latest_comment_page,
)
from books_of_time.storage.filesystem import RawPayloadFileStore

SleepFunc = Callable[[float], None | Awaitable[None]]
MonotonicFunc = Callable[[], float]


class LatestCommentsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_latest_comments(
        self, *, aid: int, offset: str = ""
    ) -> FetchResult: ...


class LatestCommentCollector:
    def __init__(
        self,
        *,
        client: LatestCommentsClient,
        raw_store: RawPayloadFileStore,
        run_id: str,
        max_scan_seconds: float = 55,
        page_retry_attempts: int = 3,
        page_retry_backoff_seconds: list[float] | None = None,
        monotonic: MonotonicFunc | None = None,
        sleep: SleepFunc | None = None,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id
        self.max_scan_seconds = max_scan_seconds
        self.page_retry_attempts = page_retry_attempts
        self.page_retry_backoff_seconds = page_retry_backoff_seconds or [1, 3, 5]
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep

    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        started_at = self.monotonic()
        configured_max_scan_seconds = task.payload.get("max_scan_seconds")
        max_scan_seconds = (
            float(configured_max_scan_seconds)
            if configured_max_scan_seconds is not None
            else self.max_scan_seconds
        )
        bvid = str(task.payload.get("bvid") or task.target_id)
        payload_cutoff = datetime.now(UTC)
        aid = await self._resolve_aid(task, session, bvid)
        now = datetime.now(UTC)
        frontier_repo = FrontierStateRepository(session)
        state = await frontier_repo.get_or_create(
            target_type="video",
            target_id=bvid,
            frontier_type="latest_comments",
            now=now,
        )
        raw_pages_before = await self._count_latest_raw_pages(session, bvid=bvid)
        comments_before = await self._count_latest_comment_observations(
            session,
            bvid=bvid,
        )
        if state.extra.get("baseline_status") == "baseline_complete":
            await self._run_incremental(
                task,
                session,
                state,
                bvid=bvid,
                aid=aid,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
            )
        elif state.extra.get("baseline_status") == "baseline_tail_complete":
            await self._run_head_sweep(
                task,
                session,
                state,
                bvid=bvid,
                aid=aid,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
            )
        else:
            await self._run_baseline_tail(
                task,
                session,
                state,
                bvid=bvid,
                aid=aid,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
            )
        await frontier_repo.save(state)
        raw_pages_after = await self._count_latest_raw_pages(session, bvid=bvid)
        comments_after = await self._count_latest_comment_observations(
            session,
            bvid=bvid,
        )
        raw_payloads_after = await session.scalar(
            select(func.count(RawPayload.id)).where(
                RawPayload.created_at >= payload_cutoff,
            )
        )
        return self._build_coverage_draft(
            task,
            state,
            raw_pages_saved=raw_pages_after - raw_pages_before,
            comments_observed=comments_after - comments_before,
            raw_payloads_saved=int(raw_payloads_after or 0),
        )

    async def _count_latest_raw_pages(
        self,
        session: AsyncSession,
        *,
        bvid: str,
    ) -> int:
        count = await session.scalar(
            select(func.count(RawPageObservation.id)).where(
                RawPageObservation.request_type == BilibiliRequestType.COMMENT_LATEST,
                RawPageObservation.target_type == "video",
                RawPageObservation.target_id == bvid,
            )
        )
        return int(count or 0)

    async def _count_latest_comment_observations(
        self,
        session: AsyncSession,
        *,
        bvid: str,
    ) -> int:
        count = await session.scalar(
            select(func.count(CommentObservation.id)).where(
                CommentObservation.bvid == bvid,
                CommentObservation.sort_mode == "latest",
            )
        )
        return int(count or 0)

    def _build_coverage_draft(
        self,
        task: CollectionTask,
        state: FrontierState,
        *,
        raw_pages_saved: int,
        comments_observed: int,
        raw_payloads_saved: int,
    ) -> CoverageDraft:
        status = state.last_scan_status or ""
        reason = self._coverage_reason(state)
        return CoverageDraft(
            task_kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=raw_pages_saved,
            pages_succeeded=raw_pages_saved,
            items_observed=comments_observed,
            raw_payloads_saved=raw_payloads_saved,
            request_errors=int(state.extra.get("failed_attempts") or 0),
            frontier_reached=status in {"baseline_complete", "incremental_complete"},
            frontier_missing=status == "frontier_missing",
            truncated=state.last_scan_truncated,
            corrupted=status in {"baseline_corrupted", "corrupted"},
            reason=reason,
            extra={
                "baseline_status": state.extra.get("baseline_status"),
                "last_scan_status": state.last_scan_status,
            },
        )

    def _coverage_reason(self, state: FrontierState) -> str:
        status = state.last_scan_status
        if status == "baseline_complete":
            return "baseline_complete"
        if status == "incremental_complete":
            return "frontier_reached"
        if status == "frontier_missing":
            return "frontier_missing"
        if status in {"baseline_paused", "paused"}:
            return "time_budget"
        if status in {"baseline_corrupted", "corrupted"}:
            if state.extra.get("failed_reason") == "cursor repeated":
                return "cursor_loop"
            return "page_retry_exhausted"
        return status or "complete"

    async def _resolve_aid(
        self,
        task: CollectionTask,
        session: AsyncSession,
        bvid: str,
    ) -> int:
        aid = task.payload.get("aid")
        if aid is not None:
            return int(aid)
        video_result = await self.client.get_video_stats(bvid)
        video_raw = await self._archive_raw(video_result, session, parser_version=None)
        try:
            video_payload = json.loads(video_result.body)
            data = video_payload.get("data") or {}
            resolved = data.get("aid")
            if resolved is None:
                raise ValueError("Video info payload does not contain data.aid")
        except Exception as exc:
            raise ParseFailure(
                request_type=video_result.request_type,
                message=str(exc),
                status_code=video_result.status_code,
                fetch_result=video_result,
            ) from exc
        task.payload = {
            **task.payload,
            "aid": int(resolved),
            "video_raw_payload_id": video_raw.id,
        }
        return int(resolved)

    async def _archive_raw(
        self,
        result: FetchResult,
        session: AsyncSession,
        *,
        parser_version: str | None,
    ) -> RawPayload:
        stored = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=".json",
        )
        return await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored,
            parser_version=parser_version,
        )

    def _time_expired(self, started_at: float, *, max_scan_seconds: float) -> bool:
        return self.monotonic() - started_at >= max_scan_seconds

    def _remaining_scan_seconds(
        self, started_at: float, *, max_scan_seconds: float
    ) -> float:
        return max_scan_seconds - (self.monotonic() - started_at)

    def _retry_backoff_seconds(self, attempt_index: int) -> float:
        return self.page_retry_backoff_seconds[
            min(attempt_index, len(self.page_retry_backoff_seconds) - 1)
        ]

    async def _sleep_for_seconds(self, seconds: float) -> None:
        result = self.sleep(seconds)
        if inspect.isawaitable(result):
            await result

    async def _fetch_page_with_retry(
        self,
        *,
        state: FrontierState,
        aid: int,
        offset: str,
        started_at: float,
        max_scan_seconds: float,
        baseline: bool,
    ) -> FetchResult | None:
        attempts = int(state.extra.get("failed_attempts") or 0)
        while attempts < self.page_retry_attempts:
            if self._time_expired(started_at, max_scan_seconds=max_scan_seconds):
                if attempts > 0 or state.extra.get("failed_cursor") == offset:
                    self._mark_paused_after_failed_attempts(
                        state,
                        cursor=offset,
                        reason=str(state.extra.get("failed_reason") or ""),
                        attempts=attempts,
                        baseline=baseline,
                    )
                else:
                    self._mark_paused(state, cursor=offset, baseline=baseline)
                return None
            try:
                result = await self.client.get_latest_comments(aid=aid, offset=offset)
                state.extra.pop("failed_cursor", None)
                state.extra.pop("failed_reason", None)
                state.extra.pop("failed_attempts", None)
                return result
            except Exception as exc:
                attempts += 1
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = str(exc)
                state.extra["failed_attempts"] = attempts
                if attempts >= self.page_retry_attempts:
                    self._mark_corrupted(state, baseline=baseline)
                    return None
                if self._time_expired(started_at, max_scan_seconds=max_scan_seconds):
                    self._mark_paused_after_failed_attempts(
                        state,
                        cursor=offset,
                        reason=str(exc),
                        attempts=attempts,
                        baseline=baseline,
                    )
                    return None
                backoff_seconds = self._retry_backoff_seconds(attempts - 1)
                if backoff_seconds > self._remaining_scan_seconds(
                    started_at, max_scan_seconds=max_scan_seconds
                ):
                    self._mark_paused_after_failed_attempts(
                        state,
                        cursor=offset,
                        reason=str(exc),
                        attempts=attempts,
                        baseline=baseline,
                    )
                    return None
                await self._sleep_for_seconds(backoff_seconds)
        self._mark_corrupted(state, baseline=baseline)
        return None

    async def _run_baseline_tail(
        self,
        task: CollectionTask,
        session: AsyncSession,
        state: FrontierState,
        *,
        bvid: str,
        aid: int,
        started_at: float,
        max_scan_seconds: float,
    ) -> None:
        extra = dict(state.extra or {})
        extra.setdefault("baseline_started_at", datetime.now(UTC).isoformat())
        extra.setdefault("baseline_status", "baseline_paused")
        seen_cursors = list(extra.get("seen_cursors") or [])
        offset = str(state.cursor or extra.get("failed_cursor") or "")
        page_number = int(state.last_scan_pages or 0) + 1
        pages_this_run = 0
        state.extra = extra

        while True:
            if pages_this_run > 0 and self._time_expired(
                started_at, max_scan_seconds=max_scan_seconds
            ):
                self._mark_paused(state, cursor=offset, baseline=True)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=True)
                return

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
                baseline=True,
            )
            if result is None:
                if state.last_scan_status == "baseline_paused":
                    await self._enqueue_followup(session, task)
                return

            parsed = await self._persist_page(
                session,
                result,
                bvid=bvid,
                aid=aid,
                page_number=page_number,
                request_offset=offset,
            )
            pages_this_run += 1
            state.last_scan_pages = int(state.last_scan_pages or 0) + 1
            seen_cursors.append(offset)
            state.extra["seen_cursors"] = seen_cursors
            if (
                state.extra.get("baseline_start_frontier_rpid") is None
                and parsed.comments
            ):
                state.extra["baseline_start_frontier_rpid"] = parsed.comments[0].rpid
                state.extra["baseline_start_frontier_time"] = (
                    result.captured_at.isoformat()
                )

            next_offset = str(parsed.extra["next_offset"])
            if parsed.extra["is_end"]:
                state.cursor = ""
                state.last_scan_at = result.captured_at
                state.last_scan_status = "baseline_tail_complete"
                state.last_scan_truncated = False
                state.extra["baseline_status"] = "baseline_tail_complete"
                return

            offset = next_offset
            state.cursor = offset
            page_number += 1

    async def _persist_page(
        self,
        session: AsyncSession,
        result: FetchResult,
        *,
        bvid: str,
        aid: int,
        page_number: int,
        request_offset: str,
    ) -> ParsedCommentPage:
        raw = await self._archive_raw(
            result,
            session,
            parser_version=COMMENT_PARSER_VERSION,
        )
        try:
            parsed = parse_latest_comment_page(
                json.loads(result.body),
                bvid=bvid,
                oid=aid,
                captured_at=result.captured_at,
                raw_payload_id=raw.id,
                page_number=page_number,
                request_offset=request_offset,
            )
        except Exception as exc:
            raise ParseFailure(
                request_type=result.request_type,
                message=str(exc),
                status_code=result.status_code,
                fetch_result=result,
            ) from exc
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_LATEST,
        )
        observations = await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
        )
        await MediaService(session).register_page_media(
            parsed=parsed,
            observations=observations,
            raw_page_id=raw_page.id,
        )
        return parsed

    async def _enqueue_followup(
        self,
        session: AsyncSession,
        task: CollectionTask,
    ) -> None:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type=task.target_type,
            target_id=task.target_id,
            priority=task.priority,
            payload={**task.payload, "mode": "latest"},
            not_before=datetime.now(UTC),
            budget_cost=task.budget_cost,
            max_retries=task.max_retries,
        )

    def _mark_paused(
        self, state: FrontierState, *, cursor: str, baseline: bool
    ) -> None:
        state.cursor = cursor
        state.last_scan_at = datetime.now(UTC)
        state.last_scan_status = "baseline_paused" if baseline else "paused"
        state.last_scan_truncated = True
        if baseline and state.extra.get("baseline_status") != "baseline_tail_complete":
            state.extra["baseline_status"] = "baseline_paused"

    def _mark_paused_after_failed_attempts(
        self,
        state: FrontierState,
        *,
        cursor: str,
        reason: str,
        attempts: int,
        baseline: bool,
    ) -> None:
        state.extra["failed_cursor"] = cursor
        state.extra["failed_reason"] = reason
        state.extra["failed_attempts"] = attempts
        self._mark_paused(state, cursor=cursor, baseline=baseline)

    def _mark_corrupted(self, state: FrontierState, *, baseline: bool) -> None:
        state.last_scan_at = datetime.now(UTC)
        state.last_scan_status = "baseline_corrupted" if baseline else "corrupted"
        state.last_scan_truncated = True
        if baseline:
            state.extra["baseline_status"] = "baseline_corrupted"

    async def _load_head_sweep_provisional_frontier(
        self,
        session: AsyncSession,
        *,
        bvid: str,
        tail_page_count: int,
    ) -> tuple[int | None, datetime | None]:
        head_page_stmt = (
            select(RawPageObservation)
            .where(
                RawPageObservation.request_type == BilibiliRequestType.COMMENT_LATEST,
                RawPageObservation.target_type == "video",
                RawPageObservation.target_id == bvid,
            )
            .order_by(RawPageObservation.captured_at.asc(), RawPageObservation.id.asc())
            .offset(max(tail_page_count, 0))
            .limit(1)
        )
        head_page = await session.scalar(head_page_stmt)
        if head_page is None:
            return None, None

        comment_stmt = (
            select(CommentObservation)
            .where(CommentObservation.raw_page_observation_id == head_page.id)
            .order_by(CommentObservation.position.asc(), CommentObservation.id.asc())
            .limit(1)
        )
        newest_comment = await session.scalar(comment_stmt)
        if newest_comment is None:
            return None, None
        return newest_comment.rpid, head_page.captured_at

    async def _load_incremental_provisional_frontier(
        self,
        session: AsyncSession,
        *,
        bvid: str,
        previous_frontier_time: datetime | None,
    ) -> tuple[int | None, datetime | None]:
        if previous_frontier_time is None:
            return None, None

        first_page_stmt = (
            select(RawPageObservation)
            .where(
                RawPageObservation.request_type == BilibiliRequestType.COMMENT_LATEST,
                RawPageObservation.target_type == "video",
                RawPageObservation.target_id == bvid,
                RawPageObservation.captured_at > previous_frontier_time,
            )
            .order_by(RawPageObservation.captured_at.asc(), RawPageObservation.id.asc())
            .limit(1)
        )
        first_page = await session.scalar(first_page_stmt)
        if first_page is None:
            return None, None

        comment_stmt = (
            select(CommentObservation)
            .where(CommentObservation.raw_page_observation_id == first_page.id)
            .order_by(CommentObservation.position.asc(), CommentObservation.id.asc())
            .limit(1)
        )
        newest_comment = await session.scalar(comment_stmt)
        if newest_comment is None:
            return None, None
        return newest_comment.rpid, first_page.captured_at

    async def _run_head_sweep(
        self,
        task: CollectionTask,
        session: AsyncSession,
        state: FrontierState,
        *,
        bvid: str,
        aid: int,
        started_at: float,
        max_scan_seconds: float,
    ) -> None:
        baseline_start_frontier_rpid = state.extra.get("baseline_start_frontier_rpid")
        if baseline_start_frontier_rpid is None:
            state.extra["failed_reason"] = "missing baseline start frontier"
            self._mark_corrupted(state, baseline=True)
            return

        offset = str(state.cursor or "")
        page_number = 1
        seen_cursors: set[str] = set()
        newest_rpid: int | None = None
        newest_captured_at: datetime | None = None
        if offset:
            (
                newest_rpid,
                newest_captured_at,
            ) = await self._load_head_sweep_provisional_frontier(
                session,
                bvid=bvid,
                tail_page_count=int(state.last_scan_pages or 0),
            )

        while True:
            if self._time_expired(started_at, max_scan_seconds=max_scan_seconds):
                self._mark_paused(state, cursor=offset, baseline=True)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=True)
                return
            seen_cursors.add(offset)

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
                baseline=True,
            )
            if result is None:
                if state.last_scan_status == "baseline_paused":
                    await self._enqueue_followup(session, task)
                return

            parsed = await self._persist_page(
                session,
                result,
                bvid=bvid,
                aid=aid,
                page_number=page_number,
                request_offset=offset,
            )
            if newest_rpid is None and parsed.comments:
                newest_rpid = parsed.comments[0].rpid
                newest_captured_at = result.captured_at

            if any(
                comment.rpid == int(baseline_start_frontier_rpid)
                for comment in parsed.comments
            ):
                state.frontier_rpid = newest_rpid or int(baseline_start_frontier_rpid)
                state.frontier_time = newest_captured_at or result.captured_at
                state.cursor = None
                state.last_scan_at = result.captured_at
                state.last_scan_status = "baseline_complete"
                state.last_scan_truncated = False
                state.extra["baseline_status"] = "baseline_complete"
                state.extra["baseline_completed_at"] = result.captured_at.isoformat()
                state.extra.pop("missing_frontier_rpid", None)
                return

            if parsed.extra["is_end"]:
                state.extra["failed_reason"] = (
                    "baseline start frontier not reached during head sweep"
                )
                self._mark_corrupted(state, baseline=True)
                return

            offset = str(parsed.extra["next_offset"])
            page_number += 1

    async def _run_incremental(
        self,
        task: CollectionTask,
        session: AsyncSession,
        state: FrontierState,
        *,
        bvid: str,
        aid: int,
        started_at: float,
        max_scan_seconds: float,
    ) -> None:
        old_frontier_rpid = state.frontier_rpid
        if old_frontier_rpid is None:
            state.extra["failed_reason"] = "missing existing frontier"
            self._mark_corrupted(state, baseline=False)
            return

        offset = str(state.cursor or "")
        page_number = 1
        seen_cursors: set[str] = set()
        newest_rpid: int | None = None
        newest_captured_at: datetime | None = None
        if offset:
            (
                newest_rpid,
                newest_captured_at,
            ) = await self._load_incremental_provisional_frontier(
                session,
                bvid=bvid,
                previous_frontier_time=state.frontier_time,
            )

        while True:
            if self._time_expired(started_at, max_scan_seconds=max_scan_seconds):
                self._mark_paused(state, cursor=offset, baseline=False)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=False)
                return
            seen_cursors.add(offset)

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
                baseline=False,
            )
            if result is None:
                if state.last_scan_status == "paused":
                    await self._enqueue_followup(session, task)
                return

            parsed = await self._persist_page(
                session,
                result,
                bvid=bvid,
                aid=aid,
                page_number=page_number,
                request_offset=offset,
            )
            if newest_rpid is None and parsed.comments:
                newest_rpid = parsed.comments[0].rpid
                newest_captured_at = result.captured_at

            if any(comment.rpid == old_frontier_rpid for comment in parsed.comments):
                state.frontier_rpid = newest_rpid or old_frontier_rpid
                state.frontier_time = newest_captured_at or result.captured_at
                state.cursor = None
                state.last_scan_at = result.captured_at
                state.last_scan_status = "incremental_complete"
                state.last_scan_truncated = False
                state.extra.pop("missing_frontier_rpid", None)
                return

            if parsed.extra["is_end"]:
                if newest_rpid is not None:
                    state.frontier_rpid = newest_rpid
                    state.frontier_time = newest_captured_at or result.captured_at
                state.cursor = None
                state.last_scan_at = result.captured_at
                state.last_scan_status = "frontier_missing"
                state.last_scan_truncated = False
                state.extra["missing_frontier_rpid"] = old_frontier_rpid
                await CommentRepository(session).mark_disappeared(
                    rpid=int(old_frontier_rpid),
                    bvid=bvid,
                    missing_reason="missing_after_seen",
                    created_at=result.captured_at,
                )
                return

            offset = str(parsed.extra["next_offset"])
            state.cursor = offset
            page_number += 1
