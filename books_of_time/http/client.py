from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from curl_cffi.requests import AsyncSession

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
        method: str,
        url: str,
        request_type: BilibiliRequestType,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)

        async with AsyncSession() as session:
            response = await session.request(
                method,
                url,
                params=params,
                headers=request_headers,
                timeout=self.timeout_seconds,
            )

        return FetchResult(
            request_type=request_type,
            method=method.upper(),
            url=url,
            params=params,
            status_code=response.status_code,
            body=response.content,
            captured_at=datetime.now(UTC),
        )
