from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

COMMENT_PARSER_VERSION = "comments.v1"


class CommentParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedComment:
    rpid: int
    oid: int
    bvid: str
    root_rpid: int | None
    parent_rpid: int | None
    author_mid: int | None
    author_name: str | None
    content: str | None
    content_hash: bytes
    like_count: int | None
    reply_count: int | None
    position: int


@dataclass(frozen=True)
class ParsedCommentPage:
    bvid: str
    oid: int
    captured_at: datetime
    raw_payload_id: int
    sort_mode: str
    page_number: int
    comments: list[ParsedComment]
    extra: dict[str, Any]


def hash_comment_content(content: str | None) -> bytes:
    normalized = (content or "").strip()
    return hashlib.sha256(normalized.encode()).digest()


def parse_hot_comment_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
) -> ParsedCommentPage:
    code = payload.get("code")
    if code not in (0, None):
        raise CommentParseError(f"Bilibili comment response code is not 0: {code}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CommentParseError("Bilibili comment response data is not an object")

    replies = data.get("replies")
    if not isinstance(replies, list):
        raise CommentParseError("Bilibili comment response data.replies is not a list")

    comments = [
        _parse_comment(
            item,
            bvid=bvid,
            fallback_oid=oid,
            position=index,
        )
        for index, item in enumerate(replies, start=1)
        if isinstance(item, dict)
    ]
    return ParsedCommentPage(
        bvid=bvid,
        oid=oid,
        captured_at=captured_at,
        raw_payload_id=raw_payload_id,
        sort_mode="hot",
        page_number=page_number,
        comments=comments,
        extra=_page_extra(data),
    )


def _parse_comment(
    item: dict[str, Any],
    *,
    bvid: str,
    fallback_oid: int,
    position: int,
) -> ParsedComment:
    content = item.get("content")
    member = item.get("member")
    message = content.get("message") if isinstance(content, dict) else None
    oid = _int_or_none(item.get("oid")) or fallback_oid
    root = _zero_as_none(_int_or_none(item.get("root")))
    parent = _zero_as_none(_int_or_none(item.get("parent")))
    author_mid = _int_or_none(member.get("mid")) if isinstance(member, dict) else None
    author_name = (
        str(member.get("uname"))
        if isinstance(member, dict) and member.get("uname") is not None
        else None
    )
    content_text = message if isinstance(message, str) else None
    return ParsedComment(
        rpid=_required_int(item.get("rpid"), "rpid"),
        oid=oid,
        bvid=bvid,
        root_rpid=root,
        parent_rpid=parent,
        author_mid=author_mid,
        author_name=author_name,
        content=content_text,
        content_hash=hash_comment_content(content_text),
        like_count=_int_or_none(item.get("like")),
        reply_count=_int_or_none(item.get("rcount")),
        position=position,
    )


def _page_extra(data: dict[str, Any]) -> dict[str, Any]:
    cursor = data.get("cursor")
    if not isinstance(cursor, dict):
        return {}
    extra: dict[str, Any] = {}
    for key in ("all_count", "is_begin", "is_end", "next", "prev"):
        if key in cursor:
            extra[key] = cursor[key]
    return extra


def _required_int(value: Any, field_name: str) -> int:
    parsed = _int_or_none(value)
    if parsed is None:
        raise CommentParseError(f"Bilibili comment field {field_name} is required")
    return parsed


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _zero_as_none(value: int | None) -> int | None:
    if value == 0:
        return None
    return value
