from datetime import UTC, datetime

import pytest

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import RequestErrorKind, RequestFailure
from books_of_time.platforms.bilibili.client import BilibiliPlatformClient
from books_of_time.platforms.bilibili.request_client import capture_bili_api_requests


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


class FakeFailingRawHttpClient:
    async def request(self, **kwargs) -> FetchResult:
        result = FetchResult(
            request_type=kwargs["request_type"],
            method=kwargs["method"],
            url=kwargs["url"],
            params=kwargs["params"],
            status_code=429,
            body=b'{"code":-429}',
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            response_headers={"Retry-After": "45"},
        )
        raise RequestFailure(
            kind=RequestErrorKind.RATE_LIMITED,
            request_type=kwargs["request_type"],
            message="rate limited",
            status_code=429,
            retry_after_seconds=45,
            fetch_result=result,
        )


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


class FakeCommentResourceType:
    VIDEO = type("VideoType", (), {"value": 1})()


class FakeCommentOrderType:
    LIKE = type("LikeOrder", (), {"value": 2})()
    TIME = type("TimeOrder", (), {"value": 3})()


class FakeComment:
    def __init__(self, oid, type_, rpid) -> None:
        self.oid = oid
        self.type_ = type_
        self.rpid = rpid

    async def get_sub_comments(self, page_index: int = 1, page_size: int = 10):
        from bilibili_api.utils.network import get_client

        response = await get_client().request(
            method="GET",
            url="https://api.bilibili.com/x/v2/reply/reply",
            params={
                "oid": self.oid,
                "type": self.type_.value,
                "root": self.rpid,
                "pn": page_index,
                "ps": page_size,
            },
            headers={},
            cookies={},
        )
        return response.json()["data"]


async def fake_get_comments(oid, type_, page_index, order):
    from bilibili_api.utils.network import get_client

    response = await get_client().request(
        method="GET",
        url="https://api.bilibili.com/x/v2/reply",
        params={
            "oid": oid,
            "type": type_.value,
            "pn": page_index,
            "sort": order.value,
        },
        headers={},
        cookies={},
    )
    return response.json()["data"]


async def fake_get_comments_lazy(oid, type_, offset, order):
    from bilibili_api.utils.network import get_client

    assert order.value == FakeCommentOrderType.TIME.value
    response = await get_client().request(
        method="GET",
        url="https://api.bilibili.com/x/v2/reply/wbi/main",
        params={
            "oid": oid,
            "type": type_.value,
            "mode": 2,
            "pagination_str": offset,
            "plat": 1,
            "seek_rpid": "",
            "web_location": 1315875,
        },
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


@pytest.mark.asyncio
async def test_hot_comments_uses_bilibili_api_client_backend(monkeypatch) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "bilibili_api.comment.CommentResourceType",
        FakeCommentResourceType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.OrderType",
        FakeCommentOrderType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.get_comments",
        fake_get_comments,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_hot_comments(aid=777, page=1)

    assert result.request_type == BilibiliRequestType.COMMENT_HOT
    assert raw_http_client.requests[0]["url"].endswith("/x/v2/reply")
    assert raw_http_client.requests[0]["params"]["oid"] == 777
    assert raw_http_client.requests[0]["params"]["pn"] == 1
    assert raw_http_client.requests[0]["params"]["sort"] == 2
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:comment_hot",
    ]


@pytest.mark.asyncio
async def test_latest_comments_uses_lazy_bilibili_api_client_backend(
    monkeypatch,
) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "bilibili_api.comment.CommentResourceType",
        FakeCommentResourceType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.OrderType",
        FakeCommentOrderType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.get_comments_lazy",
        fake_get_comments_lazy,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_latest_comments(aid=777, offset="offset-2")

    assert result.request_type == BilibiliRequestType.COMMENT_LATEST
    assert raw_http_client.requests[0]["url"].endswith("/x/v2/reply/wbi/main")
    assert raw_http_client.requests[0]["params"]["oid"] == 777
    assert raw_http_client.requests[0]["params"]["mode"] == 2
    assert raw_http_client.requests[0]["params"]["pagination_str"] == "offset-2"
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:comment_latest",
    ]


@pytest.mark.asyncio
async def test_comment_replies_uses_bilibili_api_client_backend(monkeypatch) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "bilibili_api.comment.CommentResourceType",
        FakeCommentResourceType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.Comment",
        FakeComment,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_comment_replies(
        aid=777,
        root_rpid=1001,
        page=2,
        page_size=20,
    )

    assert result.request_type == BilibiliRequestType.COMMENT_REPLY
    assert raw_http_client.requests[0]["url"].endswith("/x/v2/reply/reply")
    assert raw_http_client.requests[0]["params"]["oid"] == 777
    assert raw_http_client.requests[0]["params"]["root"] == 1001
    assert raw_http_client.requests[0]["params"]["pn"] == 2
    assert raw_http_client.requests[0]["params"]["ps"] == 20
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:comment_reply",
    ]


@pytest.mark.asyncio
async def test_bilibili_api_client_captures_failed_fetch_result() -> None:
    from bilibili_api.utils.network import get_client

    with capture_bili_api_requests(
        http_client=FakeFailingRawHttpClient(),
        rate_limiter=None,
    ) as request_context:
        with pytest.raises(RequestFailure):
            await get_client().request(
                method="GET",
                url="https://api.bilibili.com/x/v2/reply",
                params={"oid": 777},
                headers={},
                cookies={},
            )

    assert len(request_context.captured_results) == 1
    assert request_context.captured_results[0].status_code == 429
    assert (
        request_context.captured_results[0].request_type
        == BilibiliRequestType.COMMENT_REPLY
    )


@pytest.mark.asyncio
async def test_bilibili_request_context_can_disable_managed_cookies() -> None:
    from bilibili_api.utils.network import get_client

    raw_http_client = FakeRawHttpClient()
    with capture_bili_api_requests(
        http_client=raw_http_client,
        rate_limiter=None,
        use_managed_cookies=False,
    ):
        await get_client().request(
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": "BV1xx411c7mD"},
            headers={},
            cookies={"SESSDATA": "handshake-session"},
        )

    assert raw_http_client.requests[0]["use_managed_cookies"] is False
