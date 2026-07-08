from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CollectionTask, FrontierState, RawPayload
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    CommentRepository,
    FrontierStateRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.http.client import FetchResult
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

    async def collect(self, task: CollectionTask, session: AsyncSession) -> None:
        bvid = str(task.payload.get("bvid") or task.target_id)
        aid = await self._resolve_aid(task, session, bvid)
        now = datetime.now(UTC)
        frontier_repo = FrontierStateRepository(session)
        state = await frontier_repo.get_or_create(
            target_type="video",
            target_id=bvid,
            frontier_type="latest_comments",
            now=now,
        )
        if state.extra.get("baseline_status") == "baseline_complete":
            await self._run_incremental(task, session, state, bvid=bvid, aid=aid)
            await frontier_repo.save(state)
            return
        if state.extra.get("baseline_status") == "tail_complete":
            await self._run_head_sweep(task, session, state, bvid=bvid, aid=aid)
            await frontier_repo.save(state)
            return
        await self._run_baseline_tail(task, session, state, bvid=bvid, aid=aid)
        await frontier_repo.save(state)

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
        video_payload = json.loads(video_result.body)
        data = video_payload.get("data") or {}
        resolved = data.get("aid")
        if resolved is None:
            raise ValueError("Video info payload does not contain data.aid")
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

    def _time_expired(self, started_at: float) -> bool:
        return self.monotonic() - started_at >= self.max_scan_seconds

    async def _sleep_for_attempt(self, attempt_index: int) -> None:
        seconds = self.page_retry_backoff_seconds[
            min(attempt_index, len(self.page_retry_backoff_seconds) - 1)
        ]
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
        baseline: bool,
    ) -> FetchResult | None:
        attempts = int(state.extra.get("failed_attempts") or 0)
        while attempts < self.page_retry_attempts:
            if self._time_expired(started_at):
                self._mark_paused_after_failed_attempts(
                    state,
                    cursor=offset,
                    reason=str(state.extra.get("failed_reason") or ""),
                    attempts=attempts,
                    baseline=baseline,
                )
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
                if self._time_expired(started_at):
                    self._mark_paused_after_failed_attempts(
                        state,
                        cursor=offset,
                        reason=str(exc),
                        attempts=attempts,
                        baseline=baseline,
                    )
                    return None
                await self._sleep_for_attempt(attempts - 1)
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
    ) -> None:
        started_at = self.monotonic()
        extra = dict(state.extra or {})
        extra.setdefault("baseline_started_at", datetime.now(UTC).isoformat())
        extra.setdefault("baseline_status", "baseline_paused")
        seen_cursors = list(extra.get("seen_cursors") or [])
        offset = str(state.cursor or extra.get("failed_cursor") or "")
        page_number = int(state.last_scan_pages or 0) + 1
        pages_this_run = 0
        state.extra = extra

        while True:
            if pages_this_run > 0 and self._time_expired(started_at):
                self._mark_paused(state, cursor=offset, baseline=True)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=True)
                return
            seen_cursors.append(offset)
            state.extra["seen_cursors"] = seen_cursors

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
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
                state.extra["baseline_status"] = "tail_complete"
                state.extra["tail_completed_at"] = result.captured_at.isoformat()
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
        parsed = parse_latest_comment_page(
            json.loads(result.body),
            bvid=bvid,
            oid=aid,
            captured_at=result.captured_at,
            raw_payload_id=raw.id,
            page_number=page_number,
            request_offset=request_offset,
        )
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_LATEST,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
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
        if baseline:
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

    async def _run_head_sweep(
        self,
        task,
        session,
        state,
        *,
        bvid: str,
        aid: int,
    ) -> None:
        raise RuntimeError(
            "unexpected latest comment head sweep state before tail completion"
        )

    async def _run_incremental(
        self,
        task,
        session,
        state,
        *,
        bvid: str,
        aid: int,
    ) -> None:
        raise RuntimeError(
            "unexpected latest comment incremental state before baseline completion"
        )
