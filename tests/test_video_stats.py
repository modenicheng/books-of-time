from datetime import UTC, datetime

from books_of_time.parsers.video import parse_video_stats


def test_parse_video_stats_maps_bilibili_stat_payload_to_snapshot_fields() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    payload = {
        "code": 0,
        "data": {
            "bvid": "BV1abc",
            "view": 1000,
            "like": 100,
            "coin": 20,
            "favorite": 30,
            "share": 4,
            "reply": 9,
            "danmaku": 12,
        },
    }

    snapshot = parse_video_stats(payload, captured_at=captured_at, raw_payload_id=42)

    assert snapshot.bvid == "BV1abc"
    assert snapshot.captured_at == captured_at
    assert snapshot.view_count == 1000
    assert snapshot.like_count == 100
    assert snapshot.coin_count == 20
    assert snapshot.favorite_count == 30
    assert snapshot.share_count == 4
    assert snapshot.reply_count == 9
    assert snapshot.danmaku_count == 12
    assert snapshot.raw_payload_id == 42


def test_parse_video_stats_accepts_video_view_payload_with_nested_stat() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    payload = {
        "code": 0,
        "data": {
            "bvid": "BV1abc",
            "stat": {
                "view": 1000,
                "like": 100,
                "coin": 20,
                "favorite": 30,
                "share": 4,
                "reply": 9,
                "danmaku": 12,
            },
        },
    }

    snapshot = parse_video_stats(payload, captured_at=captured_at, raw_payload_id=42)

    assert snapshot.bvid == "BV1abc"
    assert snapshot.view_count == 1000
    assert snapshot.like_count == 100
