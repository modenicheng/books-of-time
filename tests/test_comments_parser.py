from datetime import UTC, datetime

import pytest

from books_of_time.parsers.comments import (
    CommentParseError,
    hash_comment_content,
    parse_hot_comment_page,
    parse_latest_comment_page,
)


def test_parse_hot_comment_page_extracts_public_comment_fields() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    payload = {
        "code": 0,
        "data": {
            "cursor": {"all_count": 2},
            "replies": [
                {
                    "rpid": 1001,
                    "oid": 777,
                    "root": 0,
                    "parent": 0,
                    "like": 12,
                    "rcount": 3,
                    "member": {"mid": "42", "uname": "Alice"},
                    "content": {"message": "first comment"},
                },
                {
                    "rpid": 1002,
                    "oid": 777,
                    "root": 1001,
                    "parent": 1001,
                    "like": 5,
                    "rcount": 0,
                    "member": {"mid": 84, "uname": "Bob"},
                    "content": {"message": "reply comment"},
                },
            ],
        },
    }

    page = parse_hot_comment_page(
        payload,
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=42,
        page_number=1,
    )

    assert page.bvid == "BV1abc"
    assert page.oid == 777
    assert page.captured_at == captured_at
    assert page.raw_payload_id == 42
    assert page.sort_mode == "hot"
    assert page.page_number == 1
    assert page.extra == {"all_count": 2}
    assert len(page.comments) == 2

    first = page.comments[0]
    assert first.rpid == 1001
    assert first.root_rpid is None
    assert first.parent_rpid is None
    assert first.author_mid == 42
    assert first.author_name == "Alice"
    assert first.content == "first comment"
    assert first.content_hash == hash_comment_content("first comment")
    assert first.like_count == 12
    assert first.reply_count == 3
    assert first.position == 1

    second = page.comments[1]
    assert second.root_rpid == 1001
    assert second.parent_rpid == 1001
    assert second.author_mid == 84
    assert second.author_name == "Bob"
    assert second.position == 2


def test_parse_hot_comment_page_rejects_missing_replies_list() -> None:
    with pytest.raises(CommentParseError, match=r"data\.replies"):
        parse_hot_comment_page(
            {"code": 0, "data": {"replies": None}},
            bvid="BV1abc",
            oid=777,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            raw_payload_id=42,
            page_number=1,
        )


def test_parse_hot_comment_page_rejects_nonzero_code() -> None:
    with pytest.raises(CommentParseError, match="code"):
        parse_hot_comment_page(
            {"code": -400, "message": "bad request", "data": {"replies": []}},
            bvid="BV1abc",
            oid=777,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            raw_payload_id=42,
            page_number=1,
        )


def latest_payload(
    *,
    replies: list[dict] | None,
    next_offset: str = "offset-2",
    is_end: bool = False,
) -> dict:
    return {
        "code": 0,
        "data": {
            "cursor": {
                "pagination_reply": {"next_offset": next_offset},
                "is_end": is_end,
            },
            "replies": replies,
        },
    }


def test_parse_latest_comment_page_extracts_cursor_and_comments() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    page = parse_latest_comment_page(
        latest_payload(
            replies=[
                {
                    "rpid": 2001,
                    "oid": 777,
                    "root": 0,
                    "parent": 0,
                    "like": 3,
                    "rcount": 0,
                    "ctime": 1783490000,
                    "member": {"mid": "42", "uname": "Alice"},
                    "content": {"message": "latest comment"},
                }
            ],
            next_offset="offset-2",
        ),
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=42,
        page_number=1,
        request_offset="",
    )

    assert page.sort_mode == "latest"
    assert page.page_number == 1
    assert page.extra["request_offset"] == ""
    assert page.extra["next_offset"] == "offset-2"
    assert page.extra["is_end"] is False
    assert page.comments[0].rpid == 2001
    assert page.comments[0].author_mid == 42
    assert page.comments[0].author_name == "Alice"
    assert page.comments[0].content == "latest comment"


def test_parse_latest_comment_page_accepts_empty_end_page() -> None:
    page = parse_latest_comment_page(
        latest_payload(replies=None, next_offset="", is_end=True),
        bvid="BV1abc",
        oid=777,
        captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        raw_payload_id=42,
        page_number=2,
        request_offset="offset-2",
    )

    assert page.comments == []
    assert page.extra["request_offset"] == "offset-2"
    assert page.extra["next_offset"] == ""
    assert page.extra["is_end"] is True


def test_parse_latest_comment_page_rejects_malformed_cursor() -> None:
    with pytest.raises(CommentParseError, match="pagination_reply"):
        parse_latest_comment_page(
            {"code": 0, "data": {"cursor": {}, "replies": []}},
            bvid="BV1abc",
            oid=777,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            raw_payload_id=42,
            page_number=1,
            request_offset="",
        )
