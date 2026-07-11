from __future__ import annotations

from bilibili_api import comment, user, video
from bilibili_api.user import VideoOrder

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult, RawHttpClient
from books_of_time.http.rate_limiter import RateLimiter
from books_of_time.platforms.bilibili.request_client import capture_bili_api_requests


class BilibiliPlatformClient:
    def __init__(
        self,
        *,
        http_client: RawHttpClient,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.http_client = http_client
        self.rate_limiter = rate_limiter

    async def get_video_stats(self, bvid: str) -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await video.Video(bvid=bvid).get_info()
            return request_context.latest_result(BilibiliRequestType.VIDEO_STATS)

    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await user.User(uid=int(mid)).get_videos(
                pn=page,
                ps=10,
                order=VideoOrder.PUBDATE,
            )
            return request_context.latest_result(BilibiliRequestType.USER_VIDEO_LIST)

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.LIKE,
            )
            return request_context.latest_result(BilibiliRequestType.COMMENT_HOT)

    async def get_latest_comments(self, *, aid: int, offset: str = "") -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await comment.get_comments_lazy(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                offset=offset,
                order=comment.OrderType.TIME,
            )
            return request_context.latest_result(BilibiliRequestType.COMMENT_LATEST)

    async def get_comment_replies(
        self,
        *,
        aid: int,
        root_rpid: int,
        page: int = 1,
        page_size: int = 20,
    ) -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await comment.Comment(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                rpid=root_rpid,
            ).get_sub_comments(page_index=page, page_size=page_size)
            return request_context.latest_result(BilibiliRequestType.COMMENT_REPLY)
