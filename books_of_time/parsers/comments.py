from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

COMMENT_PARSER_VERSION = "comments.v3"


class CommentParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCommentMedia:
    url: str
    position: int
    role: str = "comment_image"


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
    platform_created_at: datetime | None = None
    platform_time_evidence: dict[str, Any] = field(default_factory=dict)
    author_level: int | None = None
    author_official_type: int | None = None
    author_official_description: str | None = None
    author_vip_status: int | None = None
    author_vip_type: int | None = None
    author_is_senior_member: bool | None = None
    author_public_metadata_extra: dict[str, Any] = field(default_factory=dict)
    visibility: str = "visible"
    visibility_evidence: dict[str, Any] = field(default_factory=dict)
    media: list[ParsedCommentMedia] = field(default_factory=list)


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


def parse_latest_comment_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
    request_offset: str,
) -> ParsedCommentPage:
    code = payload.get("code")
    if code not in (0, None):
        raise CommentParseError(f"Bilibili comment response code is not 0: {code}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CommentParseError("Bilibili comment response data is not an object")

    cursor = data.get("cursor")
    if not isinstance(cursor, dict):
        raise CommentParseError("Bilibili latest comment cursor is not an object")

    pagination_reply = cursor.get("pagination_reply")
    if not isinstance(pagination_reply, dict):
        raise CommentParseError(
            "Bilibili latest comment cursor.pagination_reply is not an object"
        )

    next_offset = pagination_reply.get("next_offset")
    if next_offset is None:
        next_offset = ""
    if not isinstance(next_offset, str):
        raise CommentParseError(
            "Bilibili latest comment cursor.pagination_reply.next_offset is not a string"
        )

    replies = data.get("replies")
    if replies is None:
        replies = []
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
    is_end = bool(cursor.get("is_end")) or next_offset == "" or len(comments) == 0
    return ParsedCommentPage(
        bvid=bvid,
        oid=oid,
        captured_at=captured_at,
        raw_payload_id=raw_payload_id,
        sort_mode="latest",
        page_number=page_number,
        comments=comments,
        extra={
            "request_offset": request_offset,
            "next_offset": next_offset,
            "is_end": is_end,
            **_folder_extra(data),
        },
    )


def parse_comment_replies_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    root_rpid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
) -> ParsedCommentPage:
    code = payload.get("code")
    if code not in (0, None):
        raise CommentParseError(f"Bilibili comment response code is not 0: {code}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CommentParseError("Bilibili reply response data is not an object")

    replies = data.get("replies")
    if replies is None:
        replies = []
    if not isinstance(replies, list):
        raise CommentParseError("Bilibili reply response data.replies is not a list")

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
        sort_mode="reply",
        page_number=page_number,
        comments=comments,
        extra=_reply_page_extra(data, root_rpid=root_rpid),
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
    member_data = member if isinstance(member, dict) else {}
    level_info = member_data.get("level_info")
    official_verify = member_data.get("official_verify")
    vip = member_data.get("vip")
    platform_created_at, platform_time_evidence = _parse_platform_created_at(
        item.get("ctime")
    )
    content_text = message if isinstance(message, str) else None
    visibility, visibility_evidence = _parse_comment_visibility(item)
    return ParsedComment(
        rpid=_required_int(item.get("rpid"), "rpid"),
        oid=oid,
        bvid=bvid,
        root_rpid=root,
        parent_rpid=parent,
        author_mid=author_mid,
        author_name=author_name,
        platform_created_at=platform_created_at,
        platform_time_evidence=platform_time_evidence,
        author_level=(
            _int_or_none(level_info.get("current_level"))
            if isinstance(level_info, dict)
            else None
        ),
        author_official_type=(
            _int_or_none(official_verify.get("type"))
            if isinstance(official_verify, dict)
            else None
        ),
        author_official_description=(
            _text_or_none(official_verify.get("desc"))
            if isinstance(official_verify, dict)
            else None
        ),
        author_vip_status=(
            _int_or_none(vip.get("status")) if isinstance(vip, dict) else None
        ),
        author_vip_type=(
            _int_or_none(vip.get("type")) if isinstance(vip, dict) else None
        ),
        author_is_senior_member=_parse_senior_member(member_data.get("senior_member")),
        author_public_metadata_extra=_parse_public_member_metadata(member_data),
        content=content_text,
        content_hash=hash_comment_content(content_text),
        like_count=_int_or_none(item.get("like")),
        reply_count=_int_or_none(item.get("rcount")),
        position=position,
        visibility=visibility,
        visibility_evidence=visibility_evidence,
        media=_parse_comment_media(content),
    )


def _parse_platform_created_at(
    value: Any,
) -> tuple[datetime | None, dict[str, Any]]:
    if value is None:
        return None, {"status": "missing"}
    try:
        timestamp = int(value)
        if timestamp <= 0:
            raise ValueError
        return datetime.fromtimestamp(timestamp, tz=UTC), {"status": "parsed"}
    except (TypeError, ValueError, OSError, OverflowError):
        return None, {"status": "invalid", "raw_type": type(value).__name__}


def _parse_senior_member(value: Any) -> bool | None:
    if isinstance(value, dict):
        value = value.get("status")
    parsed = _int_or_none(value)
    return parsed > 0 if parsed is not None else None


def _parse_public_member_metadata(member: dict[str, Any]) -> dict[str, Any]:
    if not member:
        return {}
    metadata: dict[str, Any] = {"schema_version": "bilibili-comment-member-v1"}
    for section_name, keys in (
        ("nameplate", ("nid", "name")),
        ("pendant", ("pid", "name")),
    ):
        section = member.get(section_name)
        if not isinstance(section, dict):
            continue
        allowed = {
            key: section[key]
            for key in keys
            if isinstance(section.get(key), str | int)
            and not isinstance(section.get(key), bool)
        }
        if allowed:
            metadata[section_name] = allowed
    return metadata


def _parse_comment_visibility(
    item: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    evidence = {
        key: item[key] for key in ("folder", "invisible", "state") if key in item
    }
    folder = item.get("folder")
    if isinstance(folder, dict) and folder.get("is_folded") is True:
        return "folded", evidence
    return "visible", evidence


def _parse_comment_media(content: Any) -> list[ParsedCommentMedia]:
    if not isinstance(content, dict):
        return []

    media: list[ParsedCommentMedia] = []
    for candidate in (content.get("pictures"), content.get("picture")):
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            url = _media_url(item)
            if url is None:
                continue
            media.append(
                ParsedCommentMedia(
                    url=url,
                    position=len(media),
                    role="comment_image",
                )
            )
    return media


def _media_url(item: Any) -> str | None:
    if isinstance(item, str):
        url = item
    elif isinstance(item, dict):
        url = next(
            (
                item.get(key)
                for key in ("img_src", "url", "src")
                if isinstance(item.get(key), str)
            ),
            None,
        )
    else:
        url = None

    if not isinstance(url, str):
        return None
    stripped = url.strip()
    return stripped or None


def _page_extra(data: dict[str, Any]) -> dict[str, Any]:
    cursor = data.get("cursor")
    extra = _folder_extra(data)
    if isinstance(cursor, dict):
        for key in ("all_count", "is_begin", "is_end", "next", "prev"):
            if key in cursor:
                extra[key] = cursor[key]
    return extra


def _reply_page_extra(data: dict[str, Any], *, root_rpid: int) -> dict[str, Any]:
    extra: dict[str, Any] = {"root_rpid": root_rpid, **_folder_extra(data)}
    page = data.get("page")
    if not isinstance(page, dict):
        return extra
    for key in ("num", "size", "count"):
        if key in page:
            extra[key] = page[key]
    return extra


def _folder_extra(data: dict[str, Any]) -> dict[str, Any]:
    folder = data.get("folder")
    return {"folder": folder} if isinstance(folder, dict) else {}


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


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _zero_as_none(value: int | None) -> int | None:
    if value == 0:
        return None
    return value
