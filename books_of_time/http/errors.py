from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from books_of_time.domain.enums import BilibiliRequestType

if TYPE_CHECKING:
    from books_of_time.http.client import FetchResult


class RequestErrorKind(StrEnum):
    TIMEOUT = "timeout"
    NETWORK = "network"
    FORBIDDEN = "403"
    RATE_LIMITED = "429"
    CAPTCHA = "captcha"
    SERVER_ERROR = "5xx"
    PARSE_ERROR = "parse_error"


class RequestFailure(Exception):  # noqa: N818
    def __init__(
        self,
        *,
        kind: RequestErrorKind,
        request_type: BilibiliRequestType,
        message: str,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        fetch_result: FetchResult | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.request_type = request_type
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.fetch_result = fetch_result


class ParseFailure(RequestFailure):
    def __init__(
        self,
        *,
        request_type: BilibiliRequestType,
        message: str,
        status_code: int | None = None,
        fetch_result: FetchResult | None = None,
    ) -> None:
        super().__init__(
            kind=RequestErrorKind.PARSE_ERROR,
            request_type=request_type,
            message=message,
            status_code=status_code,
            fetch_result=fetch_result,
        )


def parse_retry_after(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    value = None
    for key, candidate in headers.items():
        if key.lower() == "retry-after":
            value = candidate
            break
    if value is None or not value.isdigit():
        return None
    return int(value)


def classify_failed_fetch(result: FetchResult) -> RequestFailure | None:
    kind = _classify_kind(result.status_code, result.body)
    if kind is None:
        return None
    return RequestFailure(
        kind=kind,
        request_type=result.request_type,
        message=f"{result.request_type.value} failed with {kind.value}",
        status_code=result.status_code,
        retry_after_seconds=parse_retry_after(result.response_headers),
        fetch_result=result,
    )


def _classify_kind(status_code: int, body: bytes) -> RequestErrorKind | None:
    text = body.decode("utf-8", errors="ignore").lower()
    if "captcha" in text or "\u9a8c\u8bc1\u7801" in text or "\u98ce\u63a7" in text:
        return RequestErrorKind.CAPTCHA

    if status_code == 403:
        return RequestErrorKind.FORBIDDEN
    if status_code == 429:
        return RequestErrorKind.RATE_LIMITED
    if status_code == 412:
        return RequestErrorKind.CAPTCHA
    if 500 <= status_code <= 599:
        return RequestErrorKind.SERVER_ERROR

    return None
