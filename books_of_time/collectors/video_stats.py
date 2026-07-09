from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import CollectionTask
from books_of_time.db.repositories import (
    RawPayloadRepository,
    VideoAvailabilitySnapshotRepository,
    VideoInfoSnapshotRepository,
    VideoMetricSnapshotRepository,
)
from books_of_time.domain.enums import TaskKind
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.parsers.video import (
    VIDEO_PARSER_VERSION,
    ParsedVideoAvailabilitySnapshot,
    parse_video_availability_snapshot,
    parse_video_info_snapshot,
    parse_video_stats,
)
from books_of_time.storage.filesystem import RawPayloadFileStore


class VideoStatsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...


class VideoStatsCollector:
    def __init__(
        self,
        *,
        client: VideoStatsClient,
        raw_store: RawPayloadFileStore,
        run_id: str,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id

    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        bvid = str(task.payload.get("bvid") or task.target_id)
        result = await self.client.get_video_stats(bvid)
        raw_repo = RawPayloadRepository(session)
        stored = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=".json",
        )
        raw = await raw_repo.insert_from_fetch_result(
            result=result,
            stored=stored,
            parser_version=VIDEO_PARSER_VERSION,
        )

        try:
            payload = json.loads(result.body)
            availability = parse_video_availability_snapshot(
                payload,
                captured_at=result.captured_at,
                raw_payload_id=raw.id,
                requested_bvid=bvid,
                http_status_code=result.status_code,
            )
            await VideoAvailabilitySnapshotRepository(session).insert_from_parsed(
                availability
            )
            if _is_target_unavailable(availability):
                return CoverageDraft(
                    task_kind=TaskKind.FETCH_VIDEO_STATS,
                    target_type=task.target_type,
                    target_id=task.target_id,
                    pages_requested=1,
                    pages_succeeded=1,
                    items_observed=0,
                    raw_payloads_saved=1,
                    reason=availability.status,
                )
            parsed = parse_video_stats(
                payload,
                captured_at=result.captured_at,
                raw_payload_id=raw.id,
            )
            info_snapshot = parse_video_info_snapshot(
                payload,
                captured_at=result.captured_at,
                raw_payload_id=raw.id,
            )
        except Exception as exc:
            raise ParseFailure(
                request_type=result.request_type,
                message=str(exc),
                status_code=result.status_code,
                fetch_result=result,
            ) from exc
        await VideoMetricSnapshotRepository(session).insert_from_parsed(parsed)
        await VideoInfoSnapshotRepository(session).insert_from_parsed(info_snapshot)
        return CoverageDraft(
            task_kind=TaskKind.FETCH_VIDEO_STATS,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=1,
            raw_payloads_saved=1,
            reason="complete",
        )


def _is_target_unavailable(availability: ParsedVideoAvailabilitySnapshot) -> bool:
    if availability.status == "visible":
        return False
    return availability.bili_code not in (0, None)
