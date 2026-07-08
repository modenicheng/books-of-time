from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.session import HttpMethod

from books_of_time.domain.enums import BilibiliRequestType


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


class RawHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        user_agent: str = "BooksOfTime/0.1 research collector",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

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
    ) -> FetchResult:
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)

        async with AsyncSession() as session:
            response = await session.request(
                method,
                url,
                params=params,
                data=data,
                headers=request_headers,
                cookies=cookies,
                allow_redirects=allow_redirects,
                timeout=self.timeout_seconds,
            )

        response_headers = {key: value for key, value in response.headers.items()}
        response_cookies = {
            cookie.name: cookie.value for cookie in getattr(response.cookies, "jar", [])
        }
        return FetchResult(
            request_type=request_type,
            method=method.upper(),
            url=str(response.url),
            params=params,
            status_code=response.status_code,
            body=response.content,
            captured_at=datetime.now(UTC),
            response_headers=response_headers,
            response_cookies=response_cookies,
        )
