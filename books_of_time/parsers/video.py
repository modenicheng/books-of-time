from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

VIDEO_PARSER_VERSION = "video_parser_v0.1.0"


@dataclass(frozen=True)
class ParsedVideoStats:
    bvid: str
    captured_at: datetime
    view_count: int | None
    like_count: int | None
    coin_count: int | None
    favorite_count: int | None
    share_count: int | None
    reply_count: int | None
    danmaku_count: int | None
    raw_payload_id: int | None


@dataclass(frozen=True)
class ParsedVideoInfoSnapshot:
    bvid: str
    captured_at: datetime
    title: str | None
    description: str | None
    owner_mid: int | None
    owner_name: str | None
    tags: dict[str, Any]
    raw_payload_id: int | None


@dataclass(frozen=True)
class ParsedVideoAvailabilitySnapshot:
    bvid: str
    captured_at: datetime
    status: str
    bili_code: int | None
    bili_message: str | None
    http_status_code: int | None
    raw_payload_id: int | None


def parse_video_stats(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    raw_payload_id: int | None,
) -> ParsedVideoStats:
    data = payload.get("data") or {}
    stats = data.get("stat") or data
    return ParsedVideoStats(
        bvid=str(data["bvid"]),
        captured_at=captured_at,
        view_count=stats.get("view"),
        like_count=stats.get("like"),
        coin_count=stats.get("coin"),
        favorite_count=stats.get("favorite"),
        share_count=stats.get("share"),
        reply_count=stats.get("reply"),
        danmaku_count=stats.get("danmaku"),
        raw_payload_id=raw_payload_id,
    )


def parse_video_availability_snapshot(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    raw_payload_id: int | None,
    requested_bvid: str,
    http_status_code: int | None,
) -> ParsedVideoAvailabilitySnapshot:
    data = payload.get("data") or {}
    code = _optional_int(payload.get("code"))
    message = payload.get("message")
    bvid = str(data.get("bvid") or requested_bvid)
    return ParsedVideoAvailabilitySnapshot(
        bvid=bvid,
        captured_at=captured_at,
        status=_availability_status(code=code, message=message, data=data),
        bili_code=code,
        bili_message=message if isinstance(message, str) else None,
        http_status_code=http_status_code,
        raw_payload_id=raw_payload_id,
    )


def parse_video_info_snapshot(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    raw_payload_id: int | None,
) -> ParsedVideoInfoSnapshot:
    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    return ParsedVideoInfoSnapshot(
        bvid=str(data["bvid"]),
        captured_at=captured_at,
        title=data.get("title"),
        description=data.get("desc") or data.get("description"),
        owner_mid=_optional_int(owner.get("mid")),
        owner_name=owner.get("name"),
        tags=_extract_tags(data),
        raw_payload_id=raw_payload_id,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _availability_status(
    *,
    code: int | None,
    message: Any,
    data: dict[str, Any],
) -> str:
    if code in (0, None) and data.get("bvid"):
        return "visible"

    text = message.lower() if isinstance(message, str) else ""
    if code == -403 or any(keyword in text for keyword in ("权限", "无权", "forbid")):
        return "permission_denied"
    if code == -404 or any(
        keyword in text for keyword in ("不存在", "删除", "not found")
    ):
        return "deleted"
    if any(keyword in text for keyword in ("不可见", "隐藏", "审核", "下架")):
        return "invisible"
    return "unknown_error"


def _extract_tags(data: dict[str, Any]) -> dict[str, Any]:
    names: list[str] = []
    source_fields: list[str] = []

    for field in ("tag", "tags"):
        if _append_tag_names(names, data.get(field)) and field not in source_fields:
            source_fields.append(field)

    tname = data.get("tname")
    if isinstance(tname, str) and tname.strip():
        if _append_unique(names, tname) and "tname" not in source_fields:
            source_fields.append("tname")

    return {"names": names, "source_fields": source_fields}


def _append_tag_names(names: list[str], entries: Any) -> bool:
    if not isinstance(entries, list):
        return False

    added = False
    for entry in entries:
        if isinstance(entry, str):
            added = _append_unique(names, entry) or added
            continue
        if not isinstance(entry, dict):
            continue
        for key in ("tag_name", "name", "title"):
            if _append_unique(names, entry.get(key)):
                added = True
                break
    return added


def _append_unique(names: list[str], value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized or normalized in names:
        return False
    names.append(normalized)
    return True
