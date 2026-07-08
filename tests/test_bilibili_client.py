from datetime import UTC, datetime

import pytest

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.platforms.bilibili.client import BilibiliPlatformClient


class FakeRawHttpClient:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, **kwargs) -> FetchResult:
        self.requests.append(kwargs)
        body = (
            b'{"code":0,"message":"OK","data":{"bvid":"BV1xx411c7mD",'
            b'"stat":{"view":10,"like":2,"coin":1,"favorite":3,"share":4,'
            b'"reply":5,"danmaku":6}}}'
        )
        return FetchResult(
            request_type=kwargs["request_type"],
            method=kwargs["method"],
            url=kwargs["url"],
            params=kwargs["params"],
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            response_headers={"content-type": "application/json"},
            response_cookies={"sid": "fake"},
        )


class FakeRateLimiter:
    def __init__(self) -> None:
        self.keys = []

    async def acquire(self, key: str) -> None:
        self.keys.append(key)


class FakeVideo:
    def __init__(self, bvid: str) -> None:
        self.bvid = bvid

    async def get_info(self) -> dict:
        from bilibili_api.utils.network import get_client

        response = await get_client().request(
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": self.bvid},
            headers={},
            cookies={},
        )
        return response.json()["data"]


class FakeUser:
    def __init__(self, uid: int) -> None:
        self.uid = uid

    async def get_videos(self, pn: int, ps: int, order) -> dict:
        from bilibili_api.utils.network import get_client

        response = await get_client().request(
            method="GET",
            url="https://api.bilibili.com/x/space/wbi/arc/search",
            params={"mid": self.uid, "pn": pn, "ps": ps, "order": order.value},
            headers={},
            cookies={},
        )
        return response.json()["data"]


@pytest.mark.asyncio
async def test_video_stats_uses_bilibili_api_client_backend(monkeypatch) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "books_of_time.platforms.bilibili.client.video.Video",
        FakeVideo,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_video_stats("BV1xx411c7mD")

    assert result.request_type == BilibiliRequestType.VIDEO_STATS
    assert b'"stat"' in result.body
    assert raw_http_client.requests[0]["url"].endswith("/x/web-interface/view")
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:video_stats",
    ]


@pytest.mark.asyncio
async def test_user_video_list_uses_bilibili_api_client_backend(monkeypatch) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "books_of_time.platforms.bilibili.client.user.User",
        FakeUser,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_user_video_list("123", page=2)

    assert result.request_type == BilibiliRequestType.USER_VIDEO_LIST
    assert raw_http_client.requests[0]["params"]["mid"] == 123
    assert raw_http_client.requests[0]["params"]["pn"] == 2
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:user_video_list",
    ]
