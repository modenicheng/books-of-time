from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.session import HttpMethod

from books_of_time.accounts.provider import CookieProvider
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.errors import (
    RequestErrorKind,
    RequestFailure,
    classify_failed_fetch,
)
from books_of_time.http.evidence import (
    HttpResponseEvidence,
    current_http_evidence_sink,
)


@dataclass(frozen=True)
class FetchResult:
    request_type: BilibiliRequestType
    method: str
    url: str
    params: dict[str, Any] | None
    status_code: int
    body: bytes
    captured_at: datetime
    response_headers: dict[str, str] | None = None
    response_cookies: dict[str, str] | None = None
    request_started_at: datetime | None = None
    request_finished_at: datetime | None = None
    response_received_at: datetime | None = None
    http_attempt_id: int | None = None


class RawHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        user_agent: str = "BooksOfTime/0.1 research collector",
        cookie_provider: CookieProvider | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.cookie_provider = cookie_provider

    async def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        request_type: BilibiliRequestType,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | str | bytes | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        allow_redirects: bool = True,
        use_managed_cookies: bool = True,
        account_id: str | None = None,
    ) -> FetchResult:
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)
        request_cookies = dict(cookies or {})
        if use_managed_cookies and self.cookie_provider is not None:
            request_cookies.update(await self.cookie_provider.get_cookies(account_id))

        evidence_sink = current_http_evidence_sink()
        request_started_at = datetime.now(UTC)
        attempt_id = None
        if evidence_sink is not None:
            attempt_id = await evidence_sink.begin(
                method=str(method),
                url=url,
                request_type=request_type,
                params=params,
                request_started_at=request_started_at,
            )

        try:
            async with AsyncSession() as session:
                response = await session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=request_headers,
                    cookies=request_cookies or None,
                    allow_redirects=allow_redirects,
                    timeout=self.timeout_seconds,
                )
                response_received_at = datetime.now(UTC)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            request_finished_at = datetime.now(UTC)
            if evidence_sink is not None and attempt_id is not None:
                await evidence_sink.record_transport_failure(
                    attempt_id,
                    request_finished_at=request_finished_at,
                    error_type=RequestErrorKind.TIMEOUT.value,
                    error_message=f"timeout ({type(exc).__name__})",
                )
            raise RequestFailure(
                kind=RequestErrorKind.TIMEOUT,
                request_type=request_type,
                message="network request timed out",
            ) from exc
        except Exception as exc:
            request_finished_at = datetime.now(UTC)
            if evidence_sink is not None and attempt_id is not None:
                await evidence_sink.record_transport_failure(
                    attempt_id,
                    request_finished_at=request_finished_at,
                    error_type=RequestErrorKind.NETWORK.value,
                    error_message=f"network failure ({type(exc).__name__})",
                )
            raise RequestFailure(
                kind=RequestErrorKind.NETWORK,
                request_type=request_type,
                message="network request failed",
            ) from exc

        request_finished_at = datetime.now(UTC)
        response_headers = {key: value for key, value in response.headers.items()}
        response_cookies = {
            cookie.name: cookie.value for cookie in getattr(response.cookies, "jar", [])
        }
        result = FetchResult(
            request_type=request_type,
            method=method.upper(),
            url=str(response.url),
            params=params,
            status_code=response.status_code,
            body=response.content,
            captured_at=response_received_at,
            response_headers=response_headers,
            response_cookies=response_cookies,
            request_started_at=request_started_at,
            request_finished_at=request_finished_at,
            response_received_at=response_received_at,
            http_attempt_id=attempt_id,
        )
        failure = classify_failed_fetch(result)
        if evidence_sink is not None and attempt_id is not None:
            content_type = next(
                (
                    value
                    for key, value in response_headers.items()
                    if key.casefold() == "content-type"
                ),
                None,
            )
            await evidence_sink.record_response(
                attempt_id,
                response=HttpResponseEvidence(
                    request_type=request_type,
                    method=result.method,
                    url=result.url,
                    params=params,
                    status_code=result.status_code,
                    body=result.body,
                    captured_at=result.captured_at,
                    request_started_at=request_started_at,
                    request_finished_at=request_finished_at,
                    response_received_at=response_received_at,
                    content_type=content_type,
                ),
                error_type=failure.kind.value if failure is not None else None,
                error_message=str(failure) if failure is not None else None,
            )
        if failure is not None:
            raise failure
        return result
