from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.http_evidence import DatabaseHttpEvidenceSink
from books_of_time.db.models import HttpRequestAttempt, RawPayload
from books_of_time.db.repositories import (
    HttpRequestAttemptRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult, RawHttpClient
from books_of_time.http.errors import RequestErrorKind, RequestFailure
from books_of_time.http.evidence import capture_http_evidence
from books_of_time.storage.base import StoredRawPayload
from books_of_time.storage.filesystem import RawPayloadFileStore


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


class _FakeCookies:
    def __init__(self) -> None:
        self.jar: list = []


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        body: bytes,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.url = "https://api.bilibili.com/x/test"
        self.headers = {"Content-Type": content_type}
        self.cookies = _FakeCookies()


class _ResponseSession:
    response = _FakeResponse(status_code=200, body=b'{"code": 0}')

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, *args, **kwargs):
        return self.response


class _RateLimitedSession(_ResponseSession):
    response = _FakeResponse(status_code=429, body=b'{"code": -509}')


class _CaptchaSession(_ResponseSession):
    response = _FakeResponse(status_code=500, body=b'{"message": "captcha"}')


class _TimeoutSession(_ResponseSession):
    async def request(self, *args, **kwargs):
        raise TimeoutError("timed out")


class _NetworkFailureSession(_ResponseSession):
    async def request(self, *args, **kwargs):
        raise OSError("connection reset")


class _FailingRawStore:
    def save(self, **kwargs):
        raise RuntimeError("raw storage unavailable")

    def read_uri(self, storage_uri: str) -> bytes:
        raise AssertionError("No raw payload should have been stored")

    def probe(self) -> str:
        return "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_type", "expected_kind", "expected_status"),
    [
        (_RateLimitedSession, RequestErrorKind.RATE_LIMITED, 429),
        (_CaptchaSession, RequestErrorKind.CAPTCHA, 500),
    ],
)
async def test_failed_http_response_is_archived_before_failure_propagates(
    monkeypatch,
    tmp_path,
    session_type,
    expected_kind,
    expected_status,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", session_type)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    raw_store = RawPayloadFileStore(tmp_path / "raw")

    async with session_factory() as session:
        sink = DatabaseHttpEvidenceSink(
            session=session,
            raw_store=raw_store,
            run_id="http-evidence",
            collection_task_id=None,
        )
        with capture_http_evidence(sink):
            with pytest.raises(RequestFailure) as exc_info:
                await RawHttpClient(timeout_seconds=1).request(
                    method="GET",
                    url="https://api.bilibili.com/x/test",
                    request_type=BilibiliRequestType.DEFAULT,
                    params={"pn": 1},
                )
        await session.commit()

    async with session_factory() as session:
        attempt = await session.scalar(select(HttpRequestAttempt))
        raw = await session.scalar(select(RawPayload))

    assert exc_info.value.kind == expected_kind
    assert exc_info.value.fetch_result is not None
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.http_status == expected_status
    assert attempt.error_type == expected_kind.value
    assert raw is not None
    assert attempt.raw_payload_id == raw.id
    assert raw_store.read_uri(raw.storage_uri) == session_type.response.content
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_response_does_not_hide_raw_storage_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", _RateLimitedSession)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        sink = DatabaseHttpEvidenceSink(
            session=session,
            raw_store=_FailingRawStore(),
            run_id="http-evidence",
            collection_task_id=None,
        )
        with capture_http_evidence(sink):
            with pytest.raises(RuntimeError, match="raw storage unavailable"):
                await RawHttpClient(timeout_seconds=1).request(
                    method="GET",
                    url="https://api.bilibili.com/x/test",
                    request_type=BilibiliRequestType.DEFAULT,
                )
        attempt = await session.scalar(select(HttpRequestAttempt))
        raw_count = await session.scalar(select(func.count(RawPayload.id)))

        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.http_status == 429
        assert attempt.error_type == "raw_storage"
        assert attempt.error_message == "raw storage failure (RuntimeError)"
        assert raw_count == 0

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_type", "expected_kind"),
    [
        (_TimeoutSession, RequestErrorKind.TIMEOUT),
        (_NetworkFailureSession, RequestErrorKind.NETWORK),
    ],
)
async def test_transport_failure_records_attempt_without_raw(
    monkeypatch,
    tmp_path,
    session_type,
    expected_kind,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", session_type)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        sink = DatabaseHttpEvidenceSink(
            session=session,
            raw_store=RawPayloadFileStore(tmp_path / "raw"),
            run_id="http-evidence",
            collection_task_id=None,
        )
        with capture_http_evidence(sink):
            with pytest.raises(RequestFailure) as exc_info:
                await RawHttpClient(timeout_seconds=1).request(
                    method="GET",
                    url="https://api.bilibili.com/x/test",
                    request_type=BilibiliRequestType.DEFAULT,
                )
        await session.commit()

    async with session_factory() as session:
        attempt = await session.scalar(select(HttpRequestAttempt))
        raw_count = await session.scalar(select(func.count(RawPayload.id)))

    assert exc_info.value.kind == expected_kind
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.error_type == expected_kind.value
    assert attempt.raw_payload_id is None
    assert raw_count == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_successful_http_attempt_waits_for_collector_raw_link(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", _ResponseSession)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    raw_store = RawPayloadFileStore(tmp_path / "raw")

    async with session_factory() as session:
        sink = DatabaseHttpEvidenceSink(
            session=session,
            raw_store=raw_store,
            run_id="http-evidence",
            collection_task_id=None,
        )
        with capture_http_evidence(sink):
            result = await RawHttpClient(timeout_seconds=1).request(
                method="GET",
                url="https://api.bilibili.com/x/test",
                request_type=BilibiliRequestType.DEFAULT,
            )
        attempt = await session.get(HttpRequestAttempt, result.http_attempt_id)
        assert attempt is not None
        assert attempt.status == "started"

        stored = raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id="http-evidence",
            suffix=".json",
        )
        raw = await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored,
        )
        assert attempt.status == "succeeded"
        assert attempt.raw_payload_id == raw.id
        await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_raw_http_client_outside_evidence_context_creates_no_attempt(
    monkeypatch,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", _ResponseSession)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    result = await RawHttpClient(timeout_seconds=1).request(
        method="GET",
        url="https://api.bilibili.com/x/test",
        request_type=BilibiliRequestType.DEFAULT,
    )
    async with session_factory() as session:
        attempt_count = await session.scalar(select(func.count(HttpRequestAttempt.id)))

    assert result.http_attempt_id is None
    assert attempt_count == 0
    await engine.dispose()
