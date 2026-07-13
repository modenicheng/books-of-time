from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from books_of_time.domain.enums import BilibiliRequestType


@dataclass(frozen=True)
class HttpResponseEvidence:
    request_type: BilibiliRequestType
    method: str
    url: str
    params: dict[str, Any] | None
    status_code: int
    body: bytes
    captured_at: datetime
    request_started_at: datetime
    request_finished_at: datetime
    response_received_at: datetime
    content_type: str | None = None


class HttpEvidenceSink(Protocol):
    async def begin(
        self,
        *,
        method: str,
        url: str,
        request_type: BilibiliRequestType,
        params: dict[str, Any] | None,
        request_started_at: datetime,
    ) -> int: ...

    async def record_response(
        self,
        attempt_id: int,
        *,
        response: HttpResponseEvidence,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    async def record_transport_failure(
        self,
        attempt_id: int,
        *,
        request_finished_at: datetime,
        error_type: str,
        error_message: str | None,
    ) -> None: ...


_current_http_evidence_sink: ContextVar[HttpEvidenceSink | None] = ContextVar(
    "books_of_time_http_evidence_sink",
    default=None,
)


@contextmanager
def capture_http_evidence(
    sink: HttpEvidenceSink | None,
) -> Iterator[HttpEvidenceSink | None]:
    token = _current_http_evidence_sink.set(sink)
    try:
        yield sink
    finally:
        _current_http_evidence_sink.reset(token)


def current_http_evidence_sink() -> HttpEvidenceSink | None:
    return _current_http_evidence_sink.get()
