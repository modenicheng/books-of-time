from __future__ import annotations

from datetime import UTC, datetime

import pytest

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult, RawHttpClient
from books_of_time.http.errors import (
    ParseFailure,
    RequestErrorKind,
    RequestFailure,
    classify_failed_fetch,
    parse_retry_after,
)


def _result(
    status_code: int,
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
) -> FetchResult:
    return FetchResult(
        request_type=BilibiliRequestType.COMMENT_HOT,
        method="GET",
        url="https://api.bilibili.com/x/v2/reply",
        params={},
        status_code=status_code,
        body=body,
        captured_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_headers=headers or {},
    )


def test_classifies_http_failure_statuses() -> None:
    assert classify_failed_fetch(_result(403)).kind == RequestErrorKind.FORBIDDEN
    assert classify_failed_fetch(_result(429)).kind == RequestErrorKind.RATE_LIMITED
    assert classify_failed_fetch(_result(503)).kind == RequestErrorKind.SERVER_ERROR


def test_classifies_captcha_and_risk_control_markers() -> None:
    assert classify_failed_fetch(_result(412)).kind == RequestErrorKind.CAPTCHA
    assert (
        classify_failed_fetch(_result(200, "需要验证码".encode())).kind
        == RequestErrorKind.CAPTCHA
    )
    assert (
        classify_failed_fetch(_result(200, "触发风控".encode())).kind
        == RequestErrorKind.CAPTCHA
    )


def test_success_response_has_no_failure() -> None:
    assert classify_failed_fetch(_result(200, b'{"code":0}')) is None


def test_retry_after_parses_integer_seconds_only() -> None:
    assert parse_retry_after({"Retry-After": "60"}) == 60
    assert parse_retry_after({"retry-after": "90"}) == 90
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) is None
    assert parse_retry_after(None) is None


def test_parse_failure_uses_parse_error_kind() -> None:
    failure = ParseFailure(
        request_type=BilibiliRequestType.VIDEO_STATS,
        message="missing data.stat",
        status_code=200,
        fetch_result=_result(200),
    )

    assert isinstance(failure, RequestFailure)
    assert failure.kind == RequestErrorKind.PARSE_ERROR
    assert failure.status_code == 200


class FakeTimeoutSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, *args, **kwargs):
        raise TimeoutError("timed out")


class FakeNetworkFailureSession(FakeTimeoutSession):
    async def request(self, *args, **kwargs):
        raise OSError("connection reset")


class FakeResponseCookies:
    def __init__(self) -> None:
        self.jar = []


class FakeResponse:
    def __init__(self) -> None:
        self.status_code = 429
        self.content = b'{"code": -429}'
        self.url = "https://api.bilibili.com/x/test"
        self.headers = {"Retry-After": "45"}
        self.cookies = FakeResponseCookies()


class FakeRateLimitedSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, *args, **kwargs):
        return FakeResponse()


class FakeSuccessfulResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__()
        self.status_code = 200
        self.content = b'{"code": 0}'
        self.headers = {}


class FakeRecordingSession:
    latest_request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, *args, **kwargs):
        type(self).latest_request = kwargs
        return FakeSuccessfulResponse()


class FakeCookieProvider:
    def __init__(self) -> None:
        self.account_ids = []

    async def get_cookies(self, account_id=None):
        self.account_ids.append(account_id)
        return {"SESSDATA": "latest-session", "bili_jct": "latest-csrf"}


@pytest.mark.asyncio
async def test_raw_http_client_maps_timeout(monkeypatch) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", FakeTimeoutSession)
    client = RawHttpClient(timeout_seconds=1)

    with pytest.raises(RequestFailure) as exc_info:
        await client.request(
            method="GET",
            url="https://api.bilibili.com/x/test",
            request_type=BilibiliRequestType.DEFAULT,
        )

    assert exc_info.value.kind == RequestErrorKind.TIMEOUT
    assert exc_info.value.request_type == BilibiliRequestType.DEFAULT
    assert exc_info.value.fetch_result is None


@pytest.mark.asyncio
async def test_raw_http_client_maps_generic_network_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "books_of_time.http.client.AsyncSession",
        FakeNetworkFailureSession,
    )
    client = RawHttpClient(timeout_seconds=1)

    with pytest.raises(RequestFailure) as exc_info:
        await client.request(
            method="GET",
            url="https://api.bilibili.com/x/test",
            request_type=BilibiliRequestType.DEFAULT,
        )

    assert exc_info.value.kind == RequestErrorKind.NETWORK
    assert exc_info.value.request_type == BilibiliRequestType.DEFAULT
    assert exc_info.value.fetch_result is None


@pytest.mark.asyncio
async def test_raw_http_client_raises_typed_failure_with_fetch_result(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "books_of_time.http.client.AsyncSession",
        FakeRateLimitedSession,
    )
    client = RawHttpClient(timeout_seconds=1)

    with pytest.raises(RequestFailure) as exc_info:
        await client.request(
            method="GET",
            url="https://api.bilibili.com/x/test",
            request_type=BilibiliRequestType.DEFAULT,
        )

    assert exc_info.value.kind == RequestErrorKind.RATE_LIMITED
    assert exc_info.value.retry_after_seconds == 45
    assert exc_info.value.fetch_result is not None
    assert exc_info.value.fetch_result.status_code == 429


@pytest.mark.asyncio
async def test_raw_http_client_injects_latest_managed_cookie_with_precedence(
    monkeypatch,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", FakeRecordingSession)
    provider = FakeCookieProvider()
    client = RawHttpClient(cookie_provider=provider)

    await client.request(
        method="GET",
        url="https://api.bilibili.com/x/test",
        request_type=BilibiliRequestType.DEFAULT,
        cookies={"SESSDATA": "stale-session", "request-only": "kept"},
        account_id="default",
    )

    assert provider.account_ids == ["default"]
    assert FakeRecordingSession.latest_request["cookies"] == {
        "SESSDATA": "latest-session",
        "bili_jct": "latest-csrf",
        "request-only": "kept",
    }


@pytest.mark.asyncio
async def test_raw_http_client_can_disable_managed_cookie_injection(
    monkeypatch,
) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", FakeRecordingSession)
    provider = FakeCookieProvider()
    client = RawHttpClient(cookie_provider=provider)

    await client.request(
        method="GET",
        url="https://api.bilibili.com/x/test",
        request_type=BilibiliRequestType.DEFAULT,
        cookies={"SESSDATA": "handshake-session"},
        use_managed_cookies=False,
    )

    assert provider.account_ids == []
    assert FakeRecordingSession.latest_request["cookies"] == {
        "SESSDATA": "handshake-session"
    }
