from datetime import UTC, datetime

from books_of_time.parsers.video import parse_video_info_snapshot, parse_video_stats


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


def test_parse_video_info_snapshot_maps_title_owner_and_tags() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    payload = {
        "code": 0,
        "data": {
            "bvid": "BV1abc",
            "title": "Demo Video",
            "desc": "A useful description",
            "owner": {"mid": 12345, "name": "Example UP"},
            "tag": [{"tag_name": "攻略"}, {"name": "游戏"}],
            "tname": "单机游戏",
        },
    }

    snapshot = parse_video_info_snapshot(
        payload,
        captured_at=captured_at,
        raw_payload_id=42,
    )

    assert snapshot.bvid == "BV1abc"
    assert snapshot.captured_at == captured_at
    assert snapshot.title == "Demo Video"
    assert snapshot.description == "A useful description"
    assert snapshot.owner_mid == 12345
    assert snapshot.owner_name == "Example UP"
    assert snapshot.tags == {
        "names": ["攻略", "游戏", "单机游戏"],
        "source_fields": ["tag", "tname"],
    }
    assert snapshot.raw_payload_id == 42


def test_parse_video_info_snapshot_accepts_missing_optional_metadata() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    snapshot = parse_video_info_snapshot(
        {"code": 0, "data": {"bvid": "BV1abc"}},
        captured_at=captured_at,
        raw_payload_id=None,
    )

    assert snapshot.title is None
    assert snapshot.description is None
    assert snapshot.owner_mid is None
    assert snapshot.owner_name is None
    assert snapshot.tags == {"names": [], "source_fields": []}
