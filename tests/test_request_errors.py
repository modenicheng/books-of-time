from __future__ import annotations

from datetime import UTC, datetime

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
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
