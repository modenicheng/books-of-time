from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.latest_scan_repositories import LatestScanRunRepository
from books_of_time.db.models import (
    CollectionTask,
    CommentScanRun,
    FrontierState,
    RawPayload,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    CommentRepository,
    FrontierStateRepository,
    FrontierStateUpdate,
    FrontierVersionConflict,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import (
    BilibiliRequestType,
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
)
from books_of_time.domain.latest_frontier import (
    anchors_from_comments,
    page_matches_anchor,
    primary_anchor,
)
from books_of_time.domain.watchlist import WatchlistPolicy
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.media.normalizer import MediaService
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedCommentPage,
    parse_latest_comment_page,
)
from books_of_time.storage.base import RawPayloadStore

SleepFunc = Callable[[float], None | Awaitable[None]]
MonotonicFunc = Callable[[], float]
NowFunc = Callable[[], datetime]


class LatestScanClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_latest_comments(
        self,
        *,
        aid: int,
        offset: str = "",
    ) -> FetchResult: ...


class LatestScanCollector:
    def __init__(
        self,
        *,
        client: LatestScanClient,
        raw_store: RawPayloadStore,
        run_id: str,
        max_scan_seconds: float = 55,
        page_retry_attempts: int = 3,
        page_retry_backoff_seconds: Sequence[float] | None = None,
        monotonic: MonotonicFunc | None = None,
        sleep: SleepFunc | None = None,
        now: NowFunc | None = None,
        watchlist_policy: WatchlistPolicy | None = None,
    ) -> None:
        if max_scan_seconds <= 0:
            raise ValueError("max_scan_seconds must be positive")
        if page_retry_attempts <= 0:
            raise ValueError("page_retry_attempts must be positive")
        backoffs = tuple(page_retry_backoff_seconds or (1, 3, 5))
        if not backoffs or any(value < 0 for value in backoffs):
            raise ValueError("page retry backoffs must be non-negative")
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id
        self.max_scan_seconds = float(max_scan_seconds)
        self.page_retry_attempts = page_retry_attempts
        self.page_retry_backoff_seconds = backoffs
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.now = now or _utc_now
        self.watchlist_policy = watchlist_policy or WatchlistPolicy()

    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        scan, frontier = await self._validate_task(task, session)
        if scan.mode not in {
            CommentScanMode.BASELINE_TAIL,
            CommentScanMode.BASELINE_HEAD_SWEEP,
        }:
            raise ValueError(f"Unsupported latest scan mode: {scan.mode.value}")

        scan_repository = LatestScanRunRepository(session)
        scan = await scan_repository.mark_running(
            scan.id,
            now=self.now(),
            oid=_optional_positive_int(task.payload.get("aid"), "aid"),
        )
        started_at = self.monotonic()
        max_scan_seconds = _positive_float(
            task.payload.get(
                "max_scan_seconds",
                scan.extra.get("max_scan_seconds", self.max_scan_seconds),
            ),
            "max_scan_seconds",
        )
        aid, aid_raw_count = await self._resolve_aid(
            task,
            session,
            scan=scan,
            bvid=scan.bvid,
        )
        if scan.oid is None:
            scan = await scan_repository.mark_running(
                scan.id,
                now=self.now(),
                oid=aid,
            )

        counters = _SliceCounters(raw_payloads_saved=aid_raw_count)
        progress = _scan_progress(frontier, scan_id=scan.id)
        cursor = str(frontier.cursor or progress.get("failed_cursor") or "")
        if scan.mode is CommentScanMode.BASELINE_HEAD_SWEEP:
            return await self._collect_head_sweep(
                task,
                session,
                scan=scan,
                frontier=frontier,
                progress=progress,
                cursor=cursor,
                counters=counters,
                aid=aid,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
            )

        while True:
            if counters.pages_succeeded > 0 and self._time_expired(
                started_at,
                max_scan_seconds=max_scan_seconds,
            ):
                frontier = await self._pause_and_enqueue(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    cursor=cursor,
                    progress=progress,
                    counters=counters,
                    aid=aid,
                    outcome="time_slice_yield",
                )
                task.payload = {
                    **task.payload,
                    "frontier_version": frontier.version,
                }
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    reason="time_slice_yield",
                )

            seen_cursors = list(progress.get("seen_cursors") or [])
            if cursor in seen_cursors:
                await self._terminalize(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    progress={
                        **progress,
                        "failed_cursor": cursor,
                        "failed_reason": "cursor repeated",
                    },
                    cursor=cursor,
                    status=CommentScanStatus.CORRUPTED,
                    outcome="cursor_loop",
                    truncated=True,
                )
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    corrupted=True,
                    reason="cursor_loop",
                )

            result = await self._fetch_with_retry(
                task,
                session,
                scan=scan,
                frontier=frontier,
                cursor=cursor,
                progress=progress,
                counters=counters,
                aid=aid,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
            )
            if result is None:
                frontier = await self._reload_frontier(session, frontier.id)
                scan = await scan_repository.lock(scan.id)
                progress = _scan_progress(frontier, scan_id=scan.id)
                if scan.status is CommentScanStatus.CORRUPTED:
                    return _coverage(
                        task,
                        counters,
                        truncated=True,
                        corrupted=True,
                        reason="retry_exhausted",
                    )
                task.payload = {
                    **task.payload,
                    "frontier_version": frontier.version,
                }
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    reason="time_slice_yield",
                )

            parsed, observation_count, frontier, scan = await self._persist_page(
                task,
                session,
                scan=scan,
                frontier=frontier,
                cursor=cursor,
                progress=progress,
                result=result,
            )
            counters.pages_succeeded += 1
            counters.items_observed += observation_count
            counters.raw_payloads_saved += 1
            progress = _scan_progress(frontier, scan_id=scan.id)
            task.payload = {
                **task.payload,
                "frontier_version": frontier.version,
            }

            next_cursor = str(parsed.extra["next_offset"])
            if parsed.extra["is_end"]:
                handoff = await scan_repository.complete_tail_and_create_head(
                    scan.id,
                    frontier_state=frontier,
                    expected_version=frontier.version,
                    now=self.now(),
                )
                if handoff is not None:
                    task.payload = {
                        **task.payload,
                        "frontier_version": handoff.frontier_state.version,
                    }
                return _coverage(
                    task,
                    counters,
                    truncated=False,
                    reason="tail_reached",
                )
            cursor = next_cursor

    async def _collect_head_sweep(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        progress: dict[str, object],
        cursor: str,
        counters: _SliceCounters,
        aid: int,
        started_at: float,
        max_scan_seconds: float,
    ) -> CoverageDraft:
        if not scan.start_anchor_set:
            raise ValueError("Baseline head sweep requires retained start anchors")
        scan_repository = LatestScanRunRepository(session)
        while True:
            if counters.pages_succeeded > 0 and self._time_expired(
                started_at,
                max_scan_seconds=max_scan_seconds,
            ):
                frontier = await self._pause_and_enqueue(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    cursor=cursor,
                    progress=progress,
                    counters=counters,
                    aid=aid,
                    outcome="time_slice_yield",
                )
                task.payload = {
                    **task.payload,
                    "frontier_version": frontier.version,
                }
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    reason="time_slice_yield",
                )

            seen_cursors = list(progress.get("seen_cursors") or [])
            if cursor in seen_cursors:
                await self._terminalize(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    progress={
                        **progress,
                        "failed_cursor": cursor,
                        "failed_reason": "cursor repeated",
                    },
                    cursor=cursor,
                    status=CommentScanStatus.CORRUPTED,
                    outcome="cursor_loop",
                    truncated=True,
                    baseline_status="baseline_corrupted",
                )
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    corrupted=True,
                    reason="cursor_loop",
                )

            result = await self._fetch_with_retry(
                task,
                session,
                scan=scan,
                frontier=frontier,
                cursor=cursor,
                progress=progress,
                counters=counters,
                aid=aid,
                started_at=started_at,
                max_scan_seconds=max_scan_seconds,
            )
            if result is None:
                frontier = await self._reload_frontier(session, frontier.id)
                scan = await scan_repository.lock(scan.id)
                if scan.status is CommentScanStatus.CORRUPTED:
                    return _coverage(
                        task,
                        counters,
                        truncated=True,
                        corrupted=True,
                        reason="retry_exhausted",
                    )
                task.payload = {
                    **task.payload,
                    "frontier_version": frontier.version,
                }
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    reason="time_slice_yield",
                )

            parsed, observation_count, frontier, scan = await self._persist_page(
                task,
                session,
                scan=scan,
                frontier=frontier,
                cursor=cursor,
                progress=progress,
                result=result,
            )
            counters.pages_succeeded += 1
            counters.items_observed += observation_count
            counters.raw_payloads_saved += 1
            progress = _scan_progress(frontier, scan_id=scan.id)
            task.payload = {
                **task.payload,
                "frontier_version": frontier.version,
            }

            if page_matches_anchor(parsed.comments, scan.start_anchor_set):
                await self._complete_head_sweep(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    progress=progress,
                )
                return _coverage(
                    task,
                    counters,
                    truncated=False,
                    reason="start_anchor_reached",
                    frontier_reached=True,
                )
            if parsed.extra["is_end"]:
                await self._terminalize(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    progress={
                        **progress,
                        "missing_start_anchor_rpids": [
                            int(item["rpid"]) for item in scan.start_anchor_set
                        ],
                    },
                    cursor="",
                    status=CommentScanStatus.CORRUPTED,
                    outcome="start_anchor_missing",
                    truncated=True,
                    baseline_status="baseline_corrupted",
                )
                return _coverage(
                    task,
                    counters,
                    truncated=True,
                    corrupted=True,
                    reason="start_anchor_missing",
                )
            cursor = str(parsed.extra["next_offset"])

    async def _validate_task(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> tuple[CommentScanRun, FrontierState]:
        if (
            task.comment_scan_run_id is None
            or task.scan_slice_no is None
            or task.scan_slice_key is None
        ):
            raise ValueError("Scan-backed latest task is missing its slice identity")
        expected_version = _non_negative_int(
            task.payload.get("frontier_version"),
            "frontier_version",
        )
        scan = await session.get(CommentScanRun, task.comment_scan_run_id)
        if scan is None:
            raise LookupError(f"Comment scan run not found: {task.comment_scan_run_id}")
        expected_slice_key = f"{scan.id}:{scan.mode.value}:{task.scan_slice_no}"
        if task.scan_slice_key != expected_slice_key:
            raise ValueError("Latest task slice identity does not match its scan run")
        if scan.bvid != str(task.payload.get("bvid") or task.target_id):
            raise ValueError("Latest task BVID does not match its scan run")
        payload_mode = task.payload.get("scan_mode")
        if payload_mode is None or str(payload_mode) != scan.mode.value:
            raise ValueError("Latest task scan mode does not match its scan run")
        if scan.status in {
            CommentScanStatus.COMPLETE,
            CommentScanStatus.PARTIAL,
            CommentScanStatus.FAILED,
            CommentScanStatus.CORRUPTED,
        }:
            raise ValueError(f"Comment scan run is terminal: {scan.status.value}")

        frontier = await session.scalar(
            select(FrontierState).where(
                FrontierState.target_type == "video",
                FrontierState.target_id == scan.bvid,
                FrontierState.frontier_type == "latest_comments",
            )
        )
        if frontier is None:
            raise LookupError(f"Latest frontier not found for {scan.bvid}")
        if frontier.active_scan_run_id != scan.id:
            raise FrontierVersionConflict(
                f"Latest scan {scan.id} no longer owns frontier {frontier.id}"
            )
        if frontier.version != expected_version:
            raise FrontierVersionConflict(
                f"Frontier state {frontier.id} version changed from {expected_version}"
            )
        return scan, frontier

    async def _resolve_aid(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        bvid: str,
    ) -> tuple[int, int]:
        if scan.oid is not None:
            task.payload = {**task.payload, "aid": scan.oid}
            return scan.oid, 0
        payload_aid = _optional_positive_int(task.payload.get("aid"), "aid")
        if payload_aid is not None:
            return payload_aid, 0

        video_result = await self.client.get_video_stats(bvid)
        video_raw = await self._archive_raw(
            video_result,
            session,
            parser_version=None,
        )
        try:
            payload = json.loads(video_result.body)
            aid = int((payload.get("data") or {})["aid"])
        except Exception as exc:
            raise ParseFailure(
                request_type=video_result.request_type,
                message=str(exc),
                status_code=video_result.status_code,
                fetch_result=video_result,
            ) from exc
        task.payload = {
            **task.payload,
            "aid": aid,
            "video_raw_payload_id": video_raw.id,
        }
        return aid, 1

    async def _fetch_with_retry(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        cursor: str,
        progress: dict[str, object],
        counters: _SliceCounters,
        aid: int,
        started_at: float,
        max_scan_seconds: float,
    ) -> FetchResult | None:
        failed_cursor = progress.get("failed_cursor")
        attempts = (
            int(progress.get("failed_attempts") or 0) if failed_cursor == cursor else 0
        )
        scan_repository = LatestScanRunRepository(session)
        while attempts < self.page_retry_attempts:
            if self._time_expired(
                started_at,
                max_scan_seconds=max_scan_seconds,
            ):
                await self._pause_and_enqueue(
                    task,
                    session,
                    scan=scan,
                    frontier=frontier,
                    cursor=cursor,
                    progress=progress,
                    counters=counters,
                    aid=aid,
                    outcome="time_slice_yield",
                )
                return None

            await scan_repository.record_page_requested(
                scan.id,
                now=self.now(),
            )
            counters.pages_requested += 1
            try:
                return await self.client.get_latest_comments(aid=aid, offset=cursor)
            except Exception as exc:
                attempts += 1
                progress = {
                    **progress,
                    "failed_cursor": cursor,
                    "failed_attempts": attempts,
                    "failed_reason": str(exc),
                }
                frontier = await self._cas_frontier(
                    task,
                    session,
                    frontier,
                    cursor=cursor,
                    progress=progress,
                    last_scan_status="running",
                    last_scan_pages=scan.pages_succeeded,
                    truncated=True,
                )
                if attempts >= self.page_retry_attempts:
                    await self._terminalize(
                        task,
                        session,
                        scan=scan,
                        frontier=frontier,
                        progress=progress,
                        cursor=cursor,
                        status=CommentScanStatus.CORRUPTED,
                        outcome="retry_exhausted",
                        truncated=True,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                    return None
                backoff = self.page_retry_backoff_seconds[
                    min(attempts - 1, len(self.page_retry_backoff_seconds) - 1)
                ]
                if self._time_expired(
                    started_at,
                    max_scan_seconds=max_scan_seconds,
                ) or backoff > self._remaining_seconds(
                    started_at,
                    max_scan_seconds=max_scan_seconds,
                ):
                    await self._pause_and_enqueue(
                        task,
                        session,
                        scan=scan,
                        frontier=frontier,
                        cursor=cursor,
                        progress=progress,
                        counters=counters,
                        aid=aid,
                        outcome="time_slice_yield",
                    )
                    return None
                await self._sleep(backoff)
        return None

    async def _persist_page(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        cursor: str,
        progress: dict[str, object],
        result: FetchResult,
    ) -> tuple[ParsedCommentPage, int, FrontierState, CommentScanRun]:
        raw = await self._archive_raw(
            result,
            session,
            parser_version=COMMENT_PARSER_VERSION,
        )
        try:
            async with session.begin_nested():
                parsed = self._parse_page(
                    result,
                    scan=scan,
                    raw_payload_id=raw.id,
                    cursor=cursor,
                )
                raw_page = await RawPageObservationRepository(
                    session
                ).insert_from_parsed_page(
                    parsed,
                    request_type=BilibiliRequestType.COMMENT_LATEST,
                    scan_run_id=scan.id,
                )
                observations = await CommentRepository(
                    session,
                    watchlist_policy=self.watchlist_policy,
                ).upsert_page(
                    parsed,
                    raw_page_observation_id=raw_page.id,
                    scan_run_id=scan.id,
                )
                await MediaService(session).register_page_media(
                    parsed=parsed,
                    observations=observations,
                    raw_page_id=raw_page.id,
                )

                anchors = list(anchors_from_comments(parsed.comments))
                if (
                    scan.mode is CommentScanMode.BASELINE_TAIL
                    and scan.pages_succeeded == 0
                ):
                    scan.start_anchor_set = anchors
                    scan.start_frontier_rpid, _ = primary_anchor(anchors)
                if (
                    scan.mode is CommentScanMode.BASELINE_HEAD_SWEEP
                    and scan.pages_succeeded == 0
                ):
                    scan.result_anchor_set = anchors
                    scan.result_frontier_rpid, _ = primary_anchor(anchors)
                    scan.extra = {
                        **scan.extra,
                        "head_captured_at": result.captured_at.isoformat(),
                    }
                next_cursor = str(parsed.extra["next_offset"])
                scan = await LatestScanRunRepository(session).record_page_succeeded(
                    scan.id,
                    result_cursor=next_cursor,
                    result_anchor_set=scan.result_anchor_set,
                    items_observed=len(observations),
                    raw_payloads_saved=1,
                    now=self.now(),
                )
                seen_cursors = list(progress.get("seen_cursors") or [])
                seen_cursors.append(cursor)
                progress = {
                    "scan_run_id": scan.id,
                    "seen_cursors": seen_cursors,
                }
                frontier = await self._cas_frontier(
                    task,
                    session,
                    frontier,
                    cursor=next_cursor,
                    progress=progress,
                    last_scan_status="running",
                    last_scan_pages=scan.pages_succeeded,
                    truncated=not bool(parsed.extra["is_end"]),
                )
        except FrontierVersionConflict:
            raise
        except Exception as exc:
            raise ParseFailure(
                request_type=result.request_type,
                message=str(exc),
                status_code=result.status_code,
                fetch_result=result,
            ) from exc
        return parsed, len(observations), frontier, scan

    async def _complete_head_sweep(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        progress: dict[str, object],
    ) -> FrontierState:
        scan = await LatestScanRunRepository(session).mark_complete(
            scan.id,
            outcome="start_anchor_reached",
            now=self.now(),
        )
        anchors = [deepcopy(item) for item in scan.result_anchor_set]
        frontier_rpid, frontier_time = primary_anchor(anchors)
        extra = deepcopy(dict(frontier.extra))
        extra.update(
            {
                "baseline_status": "baseline_complete",
                "baseline_completed_at": self.now().isoformat(),
                "latest_scan_progress": deepcopy(progress),
            }
        )
        updated = await FrontierStateRepository(session).compare_and_swap(
            frontier.id,
            frontier.version,
            FrontierStateUpdate(
                frontier_rpid=frontier_rpid,
                frontier_time=frontier_time,
                frontier_anchor_set=anchors,
                active_scan_run_id=None,
                cursor=None,
                last_scan_at=self.now(),
                last_scan_status="baseline_complete",
                last_scan_pages=scan.pages_succeeded,
                last_scan_truncated=False,
                extra=extra,
            ),
            now=self.now(),
        )
        task.payload = {**task.payload, "frontier_version": updated.version}
        return updated

    def _parse_page(
        self,
        result: FetchResult,
        *,
        scan: CommentScanRun,
        raw_payload_id: int,
        cursor: str,
    ) -> ParsedCommentPage:
        return parse_latest_comment_page(
            json.loads(result.body),
            bvid=scan.bvid,
            oid=int(scan.oid or result.params.get("oid") or 0),
            captured_at=result.captured_at,
            raw_payload_id=raw_payload_id,
            page_number=scan.pages_succeeded + 1,
            request_offset=cursor,
        )

    async def _pause_and_enqueue(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        cursor: str,
        progress: dict[str, object],
        counters: _SliceCounters,
        aid: int,
        outcome: str,
    ) -> FrontierState:
        scan = await LatestScanRunRepository(session).mark_paused(
            scan.id,
            outcome=outcome,
            now=self.now(),
        )
        frontier = await self._cas_frontier(
            task,
            session,
            frontier,
            cursor=cursor,
            progress=progress,
            last_scan_status="paused",
            last_scan_pages=scan.pages_succeeded,
            truncated=True,
        )
        await self._enqueue_next_slice(
            task,
            session,
            scan=scan,
            frontier=frontier,
            aid=aid,
        )
        return frontier

    async def _terminalize(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        progress: dict[str, object],
        cursor: str,
        status: CommentScanStatus,
        outcome: str,
        truncated: bool,
        baseline_status: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> FrontierState:
        repository = LatestScanRunRepository(session)
        if status is CommentScanStatus.COMPLETE:
            scan = await repository.mark_complete(
                scan.id,
                outcome=outcome,
                now=self.now(),
            )
        else:
            scan = await repository.mark_failed(
                scan.id,
                outcome=outcome,
                now=self.now(),
                error_type=error_type,
                error_message=error_message,
                status=status,
                truncated=truncated,
            )
        extra = deepcopy(dict(frontier.extra))
        extra["latest_scan_progress"] = deepcopy(progress)
        if baseline_status is not None:
            extra["baseline_status"] = baseline_status
        return await FrontierStateRepository(session).compare_and_swap(
            frontier.id,
            frontier.version,
            FrontierStateUpdate(
                frontier_rpid=frontier.frontier_rpid,
                frontier_time=frontier.frontier_time,
                frontier_anchor_set=frontier.frontier_anchor_set,
                active_scan_run_id=None,
                cursor=cursor,
                last_scan_at=self.now(),
                last_scan_status=baseline_status or status.value,
                last_scan_pages=scan.pages_succeeded,
                last_scan_truncated=truncated,
                extra=extra,
            ),
            now=self.now(),
        )

    async def _cas_frontier(
        self,
        task: CollectionTask,
        session: AsyncSession,
        frontier: FrontierState,
        *,
        cursor: str,
        progress: Mapping[str, object],
        last_scan_status: str,
        last_scan_pages: int,
        truncated: bool,
    ) -> FrontierState:
        extra = deepcopy(dict(frontier.extra))
        extra["latest_scan_progress"] = deepcopy(dict(progress))
        updated = await FrontierStateRepository(session).compare_and_swap(
            frontier.id,
            frontier.version,
            FrontierStateUpdate(
                frontier_rpid=frontier.frontier_rpid,
                frontier_time=frontier.frontier_time,
                frontier_anchor_set=frontier.frontier_anchor_set,
                active_scan_run_id=frontier.active_scan_run_id,
                cursor=cursor,
                last_scan_at=self.now(),
                last_scan_status=last_scan_status,
                last_scan_pages=last_scan_pages,
                last_scan_truncated=truncated,
                extra=extra,
            ),
            now=self.now(),
        )
        task.payload = {**task.payload, "frontier_version": updated.version}
        return updated

    async def _enqueue_next_slice(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        aid: int,
    ) -> CollectionTask:
        if task.scan_slice_no is None:
            raise ValueError("Latest scan task has no slice number")
        next_slice_no = task.scan_slice_no + 1
        slice_key = f"{scan.id}:{scan.mode.value}:{next_slice_no}"
        return await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type=task.target_type,
            target_id=task.target_id,
            priority=task.priority,
            budget_cost=task.budget_cost,
            payload={
                **task.payload,
                "aid": aid,
                "scan_mode": scan.mode.value,
                "frontier_version": frontier.version,
            },
            not_before=self.now(),
            max_retries=task.max_retries,
            idempotency_key=slice_key,
            snapshot_cohort_id=task.snapshot_cohort_id,
            snapshot_cohort_component_id=task.snapshot_cohort_component_id,
            comment_scan_run_id=scan.id,
            scan_slice_no=next_slice_no,
            scan_slice_key=slice_key,
        )

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

    async def _reload_frontier(
        self,
        session: AsyncSession,
        frontier_id: int,
    ) -> FrontierState:
        frontier = await session.scalar(
            select(FrontierState)
            .where(FrontierState.id == frontier_id)
            .execution_options(populate_existing=True)
        )
        if frontier is None:
            raise LookupError(f"Frontier state not found: {frontier_id}")
        return frontier

    def _time_expired(
        self,
        started_at: float,
        *,
        max_scan_seconds: float,
    ) -> bool:
        return self.monotonic() - started_at >= max_scan_seconds

    def _remaining_seconds(
        self,
        started_at: float,
        *,
        max_scan_seconds: float,
    ) -> float:
        return max_scan_seconds - (self.monotonic() - started_at)

    async def _sleep(self, seconds: float) -> None:
        result = self.sleep(seconds)
        if inspect.isawaitable(result):
            await result


class _SliceCounters:
    def __init__(
        self,
        *,
        raw_payloads_saved: int = 0,
    ) -> None:
        self.pages_requested = 0
        self.pages_succeeded = 0
        self.items_observed = 0
        self.raw_payloads_saved = raw_payloads_saved


def _scan_progress(
    frontier: FrontierState,
    *,
    scan_id: int,
) -> dict[str, object]:
    value = frontier.extra.get("latest_scan_progress")
    if not isinstance(value, dict) or value.get("scan_run_id") != scan_id:
        return {"scan_run_id": scan_id, "seen_cursors": []}
    return deepcopy(value)


def _coverage(
    task: CollectionTask,
    counters: _SliceCounters,
    *,
    truncated: bool,
    reason: str,
    corrupted: bool = False,
    frontier_reached: bool = False,
) -> CoverageDraft:
    return CoverageDraft(
        task_kind=TaskKind.FETCH_LATEST_COMMENTS,
        target_type=task.target_type,
        target_id=task.target_id,
        pages_requested=counters.pages_requested,
        pages_succeeded=counters.pages_succeeded,
        items_observed=counters.items_observed,
        raw_payloads_saved=counters.raw_payloads_saved,
        request_errors=counters.pages_requested - counters.pages_succeeded,
        frontier_reached=frontier_reached,
        frontier_missing=False,
        truncated=truncated,
        corrupted=corrupted,
        reason=reason,
        extra={
            "comment_scan_run_id": task.comment_scan_run_id,
            "scan_slice_no": task.scan_slice_no,
        },
    )


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive")
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _utc_now() -> datetime:
    return datetime.now(UTC)
