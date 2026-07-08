from __future__ import annotations

from typing import Any

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult, RawHttpClient
from books_of_time.http.rate_limiter import TokenBucketRateLimiter


class BilibiliPlatformClient:
    VIDEO_STATS_URL = "https://api.bilibili.com/x/web-interface/view"
    USER_VIDEO_LIST_URL = "https://api.bilibili.com/x/space/wbi/arc/search"

    def __init__(
        self,
        *,
        http_client: RawHttpClient,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self.http_client = http_client
        self.rate_limiter = rate_limiter

    async def get_video_stats(self, bvid: str) -> FetchResult:
        await self._acquire(BilibiliRequestType.VIDEO_STATS)
        return await self.http_client.request(
            method="GET",
            url=self.VIDEO_STATS_URL,
            request_type=BilibiliRequestType.VIDEO_STATS,
            params={"bvid": bvid},
        )

    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult:
        await self._acquire(BilibiliRequestType.USER_VIDEO_LIST)
        params: dict[str, Any] = {
            "mid": mid,
            "pn": page,
            "ps": 10,
            "order": "pubdate",
        }
        return await self.http_client.request(
            method="GET",
            url=self.USER_VIDEO_LIST_URL,
            request_type=BilibiliRequestType.USER_VIDEO_LIST,
            params=params,
        )

    async def _acquire(self, request_type: BilibiliRequestType) -> None:
        if self.rate_limiter is None:
            return
        await self.rate_limiter.acquire("global")
        await self.rate_limiter.acquire("host:bilibili")
        await self.rate_limiter.acquire(request_type.value)
