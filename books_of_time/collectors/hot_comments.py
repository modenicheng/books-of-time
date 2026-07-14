from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic as default_monotonic
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.comment_scan_repositories import CommentScanRunRepository
from books_of_time.db.models import CollectionTask, CommentScanRun, RawPayload
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    CommentRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.domain.watchlist import WatchlistPolicy
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.media.normalizer import MediaService
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedCommentPage,
    parse_hot_comment_page,
)
from books_of_time.storage.base import RawPayloadStore


class HotCommentsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult: ...


class HotCommentCollector:
    def __init__(
        self,
        *,
        client: HotCommentsClient,
        raw_store: RawPayloadStore,
        run_id: str,
        watchlist_policy: WatchlistPolicy | None = None,
        monotonic: Callable[[], float] = default_monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id
        self.watchlist_policy = watchlist_policy or WatchlistPolicy()
        self.monotonic = monotonic
        self.now = now or _utc_now

    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        if task.comment_scan_run_id is not None:
            return await self._collect_scan(task, session)
        return await self._collect_legacy(task, session)

    async def _collect_legacy(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        bvid = str(task.payload.get("bvid") or task.target_id)
        start_page = int(task.payload.get("page") or 1)
        page_limit = max(int(task.payload.get("page_limit") or 1), 1)
        aid, raw_payloads_saved = await self._resolve_aid(
            task,
            session,
            bvid=bvid,
            preferred_aid=None,
        )
        pages_succeeded = 0
        items_observed = 0

        for page in range(start_page, start_page + page_limit):
            _parsed, observations_count = await self._collect_page(
                session=session,
                bvid=bvid,
                aid=aid,
                page=page,
                scan_run_id=None,
            )
            raw_payloads_saved += 1
            pages_succeeded += 1
            items_observed += observations_count

        return CoverageDraft(
            task_kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=page_limit,
            pages_succeeded=pages_succeeded,
            items_observed=items_observed,
            raw_payloads_saved=raw_payloads_saved,
            truncated=False,
            reason="complete",
        )

    async def _collect_scan(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        if task.scan_slice_no is None or task.scan_slice_key is None:
            raise ValueError("Scan-backed hot task is missing its slice identity")
        scan_repository = CommentScanRunRepository(session)
        payload_aid = task.payload.get("aid")
        scan = await scan_repository.mark_running(
            task.comment_scan_run_id,
            now=self.now(),
            oid=int(payload_aid) if payload_aid is not None else None,
        )
        payload_mode = task.payload.get("scan_mode")
        if payload_mode is not None and str(payload_mode) != scan.mode.value:
            raise ValueError("Hot task scan mode does not match its scan run")
        bvid = str(task.payload.get("bvid") or task.target_id)
        aid, raw_payloads_saved = await self._resolve_aid(
            task,
            session,
            bvid=bvid,
            preferred_aid=scan.oid,
        )
        if scan.oid is None:
            scan = await scan_repository.mark_running(
                scan.id,
                now=self.now(),
                oid=aid,
            )

        start_page, end_page = _scan_page_range(scan)
        _validate_payload_range(task.payload, start_page=start_page, end_page=end_page)
        max_pages_per_slice = _positive_int(
            task.payload.get(
                "max_pages_per_slice",
                scan.extra.get("max_pages_per_slice", 10),
            ),
            "max_pages_per_slice",
        )
        max_scan_seconds = _positive_float(
            task.payload.get(
                "max_scan_seconds",
                scan.extra.get("max_scan_seconds", 55),
            ),
            "max_scan_seconds",
        )
        slice_started = self.monotonic()
        pages_requested = 0
        pages_succeeded = 0
        items_observed = 0

        while True:
            page = scan.next_page_number
            if page is None:
                raise ValueError("Hot comment scan has no next page number")
            if page > end_page:
                await scan_repository.mark_complete(
                    scan.id,
                    outcome="target_reached",
                    now=self.now(),
                )
                return _scan_coverage(
                    task,
                    pages_requested=pages_requested,
                    pages_succeeded=pages_succeeded,
                    items_observed=items_observed,
                    raw_payloads_saved=raw_payloads_saved,
                    truncated=False,
                    reason="target_reached",
                )
            if pages_succeeded > 0 and (
                pages_succeeded >= max_pages_per_slice
                or self.monotonic() - slice_started >= max_scan_seconds
            ):
                scan = await scan_repository.mark_paused(
                    scan.id,
                    outcome="time_slice_yield",
                    now=self.now(),
                )
                await self._enqueue_next_slice(
                    task,
                    session,
                    scan=scan,
                    aid=aid,
                )
                return _scan_coverage(
                    task,
                    pages_requested=pages_requested,
                    pages_succeeded=pages_succeeded,
                    items_observed=items_observed,
                    raw_payloads_saved=raw_payloads_saved,
                    truncated=True,
                    reason="time_slice_yield",
                )

            await scan_repository.record_page_requested(
                scan.id,
                page_number=page,
                now=self.now(),
            )
            pages_requested += 1
            try:
                parsed, observation_count = await self._collect_page(
                    session=session,
                    bvid=bvid,
                    aid=aid,
                    page=page,
                    scan_run_id=scan.id,
                )
                scan = await scan_repository.record_page_succeeded(
                    scan.id,
                    page_number=page,
                    items_observed=observation_count,
                    raw_payloads_saved=1,
                    now=self.now(),
                )
            except Exception as exc:
                await scan_repository.record_page_failed(
                    scan.id,
                    page_number=page,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    now=self.now(),
                )
                raise

            pages_succeeded += 1
            items_observed += observation_count
            raw_payloads_saved += 1
            if parsed.extra.get("is_end") is True or not parsed.comments:
                await scan_repository.mark_complete(
                    scan.id,
                    outcome="server_end",
                    now=self.now(),
                )
                return _scan_coverage(
                    task,
                    pages_requested=pages_requested,
                    pages_succeeded=pages_succeeded,
                    items_observed=items_observed,
                    raw_payloads_saved=raw_payloads_saved,
                    truncated=False,
                    reason="server_end",
                )

    async def _resolve_aid(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        bvid: str,
        preferred_aid: int | None,
    ) -> tuple[int, int]:
        aid = preferred_aid
        if aid is None and task.payload.get("aid") is not None:
            aid = int(task.payload["aid"])
        if aid is not None:
            task.payload = {**task.payload, "aid": aid}
            return aid, 0

        video_result = await self.client.get_video_stats(bvid)
        video_raw = await self._archive_raw(video_result, session)
        try:
            video_payload = json.loads(video_result.body)
            aid = _extract_aid(video_payload)
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

    async def _enqueue_next_slice(
        self,
        task: CollectionTask,
        session: AsyncSession,
        *,
        scan: CommentScanRun,
        aid: int,
    ) -> CollectionTask:
        if task.scan_slice_no is None or scan.next_page_number is None:
            raise ValueError("Cannot continue a hot scan without slice/page state")
        next_slice_no = task.scan_slice_no + 1
        return await CollectionTaskRepository(session).enqueue(
            kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            priority=task.priority,
            budget_cost=task.budget_cost,
            payload={
                **task.payload,
                "aid": aid,
                "page": scan.next_page_number,
            },
            not_before=self.now(),
            max_retries=task.max_retries,
            idempotency_key=(
                f"{scan.scan_key}:{scan.mode.value}:active:{next_slice_no}"
            ),
            snapshot_cohort_id=task.snapshot_cohort_id,
            snapshot_cohort_component_id=task.snapshot_cohort_component_id,
            comment_scan_run_id=scan.id,
            scan_slice_no=next_slice_no,
            scan_slice_key=f"{scan.id}:{scan.mode.value}:{next_slice_no}",
        )

    async def _collect_page(
        self,
        *,
        session: AsyncSession,
        bvid: str,
        aid: int,
        page: int,
        scan_run_id: int | None,
    ) -> tuple[ParsedCommentPage, int]:
        comments_result = await self.client.get_hot_comments(aid=aid, page=page)
        comments_raw = await self._archive_raw(comments_result, session)
        try:
            parsed = self._parse_page(
                comments_result,
                bvid=bvid,
                aid=aid,
                page=page,
                raw_payload_id=comments_raw.id,
            )
        except Exception as exc:
            raise ParseFailure(
                request_type=comments_result.request_type,
                message=str(exc),
                status_code=comments_result.status_code,
                fetch_result=comments_result,
            ) from exc
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
            scan_run_id=scan_run_id,
        )
        observations = await CommentRepository(
            session,
            watchlist_policy=self.watchlist_policy,
        ).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
            scan_run_id=scan_run_id,
        )
        await MediaService(session).register_page_media(
            parsed=parsed,
            observations=observations,
            raw_page_id=raw_page.id,
        )
        return parsed, len(observations)

    def _parse_page(
        self,
        result: FetchResult,
        *,
        bvid: str,
        aid: int,
        page: int,
        raw_payload_id: int,
    ) -> ParsedCommentPage:
        return parse_hot_comment_page(
            json.loads(result.body),
            bvid=bvid,
            oid=aid,
            captured_at=result.captured_at,
            raw_payload_id=raw_payload_id,
            page_number=page,
        )

    async def _archive_raw(
        self,
        result: FetchResult,
        session: AsyncSession,
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
            parser_version=COMMENT_PARSER_VERSION
            if result.request_type == BilibiliRequestType.COMMENT_HOT
            else None,
        )


def _extract_aid(payload: dict) -> int:
    data = payload.get("data") or {}
    aid = data.get("aid")
    if aid is None:
        raise ValueError("Video info payload does not contain data.aid")
    return int(aid)


def _scan_coverage(
    task: CollectionTask,
    *,
    pages_requested: int,
    pages_succeeded: int,
    items_observed: int,
    raw_payloads_saved: int,
    truncated: bool,
    reason: str,
) -> CoverageDraft:
    return CoverageDraft(
        task_kind=TaskKind.FETCH_HOT_COMMENTS,
        target_type=task.target_type,
        target_id=task.target_id,
        pages_requested=pages_requested,
        pages_succeeded=pages_succeeded,
        items_observed=items_observed,
        raw_payloads_saved=raw_payloads_saved,
        truncated=truncated,
        reason=reason,
        extra={
            "comment_scan_run_id": task.comment_scan_run_id,
            "scan_slice_no": task.scan_slice_no,
        },
    )


def _scan_page_range(scan: CommentScanRun) -> tuple[int, int]:
    try:
        start_page = int(scan.extra["start_page"])
        end_page = int(scan.extra["end_page"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Hot comment scan is missing its page range") from exc
    if start_page <= 0 or end_page < start_page:
        raise ValueError("Hot comment scan has an invalid page range")
    return start_page, end_page


def _validate_payload_range(
    payload: dict[str, Any],
    *,
    start_page: int,
    end_page: int,
) -> None:
    for key, expected in (("start_page", start_page), ("end_page", end_page)):
        if key in payload and int(payload[key]) != expected:
            raise ValueError(f"Hot task {key} does not match its scan run")


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive")
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _utc_now() -> datetime:
    return datetime.now(UTC)
