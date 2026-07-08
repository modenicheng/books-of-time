from books_of_time.platforms.bilibili.client import BilibiliPlatformClient


def test_video_stats_client_uses_video_view_endpoint() -> None:
    assert BilibiliPlatformClient.VIDEO_STATS_URL.endswith("/x/web-interface/view")
