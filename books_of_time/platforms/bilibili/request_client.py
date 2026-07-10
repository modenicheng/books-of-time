from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from bilibili_api.utils.network import (
    BiliAPIClient,
    BiliAPIFile,
    BiliAPIResponse,
    BiliWsMsgType,
    register_client,
    select_client,
)

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult, RawHttpClient
from books_of_time.http.errors import RequestFailure
from books_of_time.http.rate_limiter import TokenBucketRateLimiter
from books_of_time.platforms.bilibili.requests import classify_bilibili_request

CLIENT_NAME = "books_of_time"


@dataclass
class BiliAPIRequestContext:
    http_client: RawHttpClient
    rate_limiter: TokenBucketRateLimiter | None = None
    use_managed_cookies: bool = True
    captured_results: list[FetchResult] = field(default_factory=list)

    def latest_result(self, request_type: BilibiliRequestType) -> FetchResult:
        for result in reversed(self.captured_results):
            if result.request_type == request_type:
                return result
        msg = f"No captured Bilibili request for {request_type.value}"
        raise RuntimeError(msg)


_current_context: ContextVar[BiliAPIRequestContext | None] = ContextVar(
    "books_of_time_bili_api_request_context",
    default=None,
)

_registered = False


def ensure_books_of_time_client_registered() -> None:
    global _registered
    if not _registered:
        register_client(CLIENT_NAME, BooksOfTimeBiliAPIClient)
        _registered = True
    else:
        select_client(CLIENT_NAME)


@contextmanager
def capture_bili_api_requests(
    *,
    http_client: RawHttpClient,
    rate_limiter: TokenBucketRateLimiter | None,
    use_managed_cookies: bool = True,
) -> Iterator[BiliAPIRequestContext]:
    ensure_books_of_time_client_registered()
    context = BiliAPIRequestContext(
        http_client=http_client,
        rate_limiter=rate_limiter,
        use_managed_cookies=use_managed_cookies,
    )
    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


class BooksOfTimeBiliAPIClient(BiliAPIClient):
    def __init__(
        self,
        proxy: str = "",
        timeout: float = 0.0,
        verify_ssl: bool = True,
        trust_env: bool = True,
        session: object | None = None,
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.trust_env = trust_env
        self.session = session

    def get_wrapped_session(self) -> object:
        return self.session

    def set_timeout(self, timeout: float = 0.0) -> None:
        self.timeout = timeout

    def set_proxy(self, proxy: str = "") -> None:
        self.proxy = proxy

    def set_verify_ssl(self, verify_ssl: bool = True) -> None:
        self.verify_ssl = verify_ssl

    def set_trust_env(self, trust_env: bool = True) -> None:
        self.trust_env = trust_env

    async def request(
        self,
        method: str = "",
        url: str = "",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | str | bytes | None = None,
        files: dict[str, BiliAPIFile] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> BiliAPIResponse:
        _params = params or {}
        _data: dict[str, Any] | str | bytes = {} if data is None else data
        _headers = headers or {}
        _cookies = cookies or {}
        _files = files or {}
        if _files:
            raise NotImplementedError(
                "Bilibili file uploads are outside collector scope"
            )

        context = _current_context.get()
        if context is None:
            msg = "BooksOfTimeBiliAPIClient requires capture_bili_api_requests context"
            raise RuntimeError(msg)

        request_type = classify_bilibili_request(url, _params)
        await _acquire(context.rate_limiter, request_type)
        try:
            result = await context.http_client.request(
                method=method,
                url=url,
                request_type=request_type,
                params=_params,
                data=None if _data == {} else _data,
                headers=_headers,
                cookies=_cookies,
                allow_redirects=allow_redirects,
                use_managed_cookies=context.use_managed_cookies,
            )
        except RequestFailure as exc:
            if exc.fetch_result is not None:
                context.captured_results.append(exc.fetch_result)
            raise
        context.captured_results.append(result)
        return BiliAPIResponse(
            code=result.status_code,
            headers=result.response_headers,
            cookies=result.response_cookies,
            raw=result.body,
            url=result.url,
        )

    async def download_create(self, url: str = "", headers: dict = {}) -> int:
        raise NotImplementedError("Downloads are outside collector scope")

    async def download_chunk(self, cnt: int) -> bytes:
        raise NotImplementedError("Downloads are outside collector scope")

    def download_content_length(self, cnt: int) -> int:
        raise NotImplementedError("Downloads are outside collector scope")

    async def download_close(self, cnt: int) -> None:
        raise NotImplementedError("Downloads are outside collector scope")

    async def ws_create(
        self,
        url: str = "",
        params: dict = {},
        headers: dict = {},
    ) -> int:
        raise NotImplementedError("WebSockets are outside collector scope")

    async def ws_send(self, cnt: int, data: bytes) -> None:
        raise NotImplementedError("WebSockets are outside collector scope")

    async def ws_recv(self, cnt: int) -> tuple[bytes, BiliWsMsgType]:
        raise NotImplementedError("WebSockets are outside collector scope")

    async def ws_close(self, cnt: int) -> None:
        raise NotImplementedError("WebSockets are outside collector scope")

    async def close(self) -> None:
        return None


async def _acquire(
    rate_limiter: TokenBucketRateLimiter | None,
    request_type: BilibiliRequestType,
) -> None:
    if rate_limiter is None:
        return
    await rate_limiter.acquire("global")
    await rate_limiter.acquire("host:bilibili")
    await rate_limiter.acquire(request_type.value)
