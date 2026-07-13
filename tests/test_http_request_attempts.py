from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import HttpRequestAttempt
from books_of_time.db.repositories import (
    HttpRequestAttemptRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.storage.base import StoredRawPayload


def _stored(body: bytes, suffix: str) -> StoredRawPayload:
    return StoredRawPayload(
        storage_uri=f"file:///raw/{suffix}.json.zst",
        payload_hash_hex=hashlib.sha256(body).hexdigest(),
        compressed_size=len(body),
        uncompressed_size=len(body),
    )


def _fetch_result(
    *,
    attempt: HttpRequestAttempt,
    status_code: int,
    body: bytes,
    started_at: datetime,
    finished_at: datetime,
) -> FetchResult:
    return FetchResult(
        request_type=BilibiliRequestType.VIDEO_STATS,
        method="GET",
        url="https://api.bilibili.com/x/web-interface/view",
        params={"bvid": "BV-EVIDENCE"},
        status_code=status_code,
        body=body,
        captured_at=finished_at,
        request_started_at=started_at,
        request_finished_at=finished_at,
        response_received_at=finished_at,
        http_attempt_id=attempt.id,
    )


@pytest.mark.asyncio
async def test_http_request_attempt_repository_covers_terminal_lifecycle() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    started_at = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(milliseconds=1250)
    url = "https://api.bilibili.com/x/web-interface/view?secret=not-stored"
    params = {"bvid": "BV-EVIDENCE", "pn": 1}

    async with session_factory() as session:
        repository = HttpRequestAttemptRepository(session)
        succeeded = await repository.begin(
            collection_task_id=None,
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="get",
            url=url,
            params=params,
            attempt_started_at=started_at,
            request_started_at=started_at,
        )

        assert succeeded.status == "started"
        assert succeeded.raw_payload_id is None
        assert succeeded.method == "GET"
        assert succeeded.url_hash == hashlib.sha256(url.encode()).digest()
        canonical_params = json.dumps(
            params,
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
        assert succeeded.params_hash == hashlib.sha256(canonical_params).digest()
        assert "url" not in HttpRequestAttempt.__table__.c
        assert "params" not in HttpRequestAttempt.__table__.c

        await repository.record_response(
            succeeded.id,
            http_status=200,
            request_finished_at=finished_at,
            response_received_at=finished_at,
        )
        assert succeeded.status == "started"
        assert succeeded.duration_ms == 1250

        success_body = b'{"code": 0}'
        success_raw = await RawPayloadRepository(session).insert_from_fetch_result(
            result=_fetch_result(
                attempt=succeeded,
                status_code=200,
                body=success_body,
                started_at=started_at,
                finished_at=finished_at,
            ),
            stored=_stored(success_body, "success"),
        )
        assert succeeded.status == "succeeded"
        assert succeeded.raw_payload_id == success_raw.id

        rate_limited = await repository.begin(
            collection_task_id=None,
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url=url,
            params={"pn": 1, "bvid": "BV-EVIDENCE"},
            attempt_started_at=started_at,
            request_started_at=started_at,
        )
        await repository.record_response(
            rate_limited.id,
            http_status=429,
            request_finished_at=finished_at,
            response_received_at=finished_at,
            error_type="429",
            error_message=f"  {'x' * 2100}  ",
        )
        failed_body = b'{"code": -509}'
        failed_raw = await RawPayloadRepository(session).insert_from_fetch_result(
            result=_fetch_result(
                attempt=rate_limited,
                status_code=429,
                body=failed_body,
                started_at=started_at,
                finished_at=finished_at,
            ),
            stored=_stored(failed_body, "failed"),
            attempt_status="failed",
        )
        assert rate_limited.status == "failed"
        assert rate_limited.error_type == "429"
        assert rate_limited.error_message == "x" * 2000
        assert rate_limited.raw_payload_id == failed_raw.id
        assert rate_limited.params_hash == succeeded.params_hash

        timed_out = await repository.begin(
            collection_task_id=None,
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url=url,
            params=params,
            attempt_started_at=started_at,
            request_started_at=started_at,
        )
        await repository.record_transport_failure(
            timed_out.id,
            error_type="timeout",
            error_message="  connection timed out  ",
            request_finished_at=finished_at,
        )
        assert timed_out.status == "failed"
        assert timed_out.raw_payload_id is None
        assert timed_out.error_message == "connection timed out"

        abandoned = await repository.begin(
            collection_task_id=None,
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url=url,
            params=params,
            attempt_started_at=started_at,
            request_started_at=started_at,
        )
        await repository.mark_abandoned(
            abandoned.id,
            finished_at=finished_at,
            error_message="collector aborted before raw persistence",
        )
        assert abandoned.status == "abandoned"
        assert abandoned.error_type == "collector_abort"
        assert abandoned.raw_payload_id is None
        await session.commit()

    await engine.dispose()
