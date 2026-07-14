from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.cohort_repositories import SnapshotCohortExecutionRepository
from books_of_time.db.repositories import (
    HttpRequestAttemptRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.http.evidence import HttpResponseEvidence
from books_of_time.storage.base import RawPayloadStore


class DatabaseHttpEvidenceSink:
    def __init__(
        self,
        *,
        session: AsyncSession,
        raw_store: RawPayloadStore,
        run_id: str,
        collection_task_id: int | None,
        snapshot_cohort_id: int | None = None,
        snapshot_cohort_component_id: int | None = None,
    ) -> None:
        self.session = session
        self.raw_store = raw_store
        self.run_id = run_id
        self.collection_task_id = collection_task_id
        self.snapshot_cohort_id = snapshot_cohort_id
        self.snapshot_cohort_component_id = snapshot_cohort_component_id
        self._attempt_ids: set[int] = set()

    async def begin(
        self,
        *,
        method: str,
        url: str,
        request_type: BilibiliRequestType,
        params: dict[str, Any] | None,
        request_started_at: datetime,
    ) -> int:
        attempt = await HttpRequestAttemptRepository(self.session).begin(
            collection_task_id=self.collection_task_id,
            snapshot_cohort_id=self.snapshot_cohort_id,
            snapshot_cohort_component_id=self.snapshot_cohort_component_id,
            request_type=request_type,
            method=method,
            url=url,
            params=params,
            attempt_started_at=request_started_at,
            request_started_at=request_started_at,
        )
        await SnapshotCohortExecutionRepository(
            self.session
        ).record_http_attempt_started(attempt)
        self._attempt_ids.add(attempt.id)
        return attempt.id

    async def record_response(
        self,
        attempt_id: int,
        *,
        response: HttpResponseEvidence,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        repository = HttpRequestAttemptRepository(self.session)
        await repository.record_response(
            attempt_id,
            http_status=response.status_code,
            request_finished_at=response.request_finished_at,
            response_received_at=response.response_received_at,
            error_type=error_type,
            error_message=error_message,
        )
        if error_type is None:
            return

        try:
            stored = self.raw_store.save(
                body=response.body,
                captured_at=response.captured_at,
                run_id=self.run_id,
                suffix=_raw_suffix(response.content_type),
            )
        except Exception as exc:
            await repository.record_transport_failure(
                attempt_id,
                error_type="raw_storage",
                error_message=f"raw storage failure ({type(exc).__name__})",
                request_finished_at=response.request_finished_at,
            )
            raise
        result = FetchResult(
            request_type=response.request_type,
            method=response.method,
            url=response.url,
            params=response.params,
            status_code=response.status_code,
            body=response.body,
            captured_at=response.captured_at,
            request_started_at=response.request_started_at,
            request_finished_at=response.request_finished_at,
            response_received_at=response.response_received_at,
            http_attempt_id=attempt_id,
        )
        await RawPayloadRepository(self.session).insert_from_fetch_result(
            result=result,
            stored=stored,
            attempt_status="failed",
        )

    async def record_transport_failure(
        self,
        attempt_id: int,
        *,
        request_finished_at: datetime,
        error_type: str,
        error_message: str | None,
    ) -> None:
        await HttpRequestAttemptRepository(self.session).record_transport_failure(
            attempt_id,
            error_type=error_type,
            error_message=error_message,
            request_finished_at=request_finished_at,
        )

    async def mark_abandoned(
        self,
        *,
        finished_at: datetime,
        error_message: str | None,
    ) -> None:
        repository = HttpRequestAttemptRepository(self.session)
        for attempt_id in self._attempt_ids:
            await repository.mark_abandoned(
                attempt_id,
                finished_at=finished_at,
                error_message=error_message,
            )


def _raw_suffix(content_type: str | None) -> str:
    if content_type is not None and "json" in content_type.casefold():
        return ".json"
    return ".bin"
