from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EVENT_TARGET_TYPES = frozenset({"uid", "keyword", "seed_bvid", "game"})
EVENT_STATUSES = frozenset({"planned", "active", "closed", "archived"})

_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")


@dataclass(frozen=True, slots=True)
class EventTimelineRow:
    event_id: int
    event_slug: str
    timestamp: datetime
    record_type: str
    source_table: str
    source_key: str
    bvid: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "event-timeline-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "timestamp": self.timestamp.isoformat(),
            "record_type": self.record_type,
            "source_table": self.source_table,
            "source_key": self.source_key,
            "bvid": self.bvid,
            "data": self.data,
        }


def normalize_event_slug(value: str) -> str:
    normalized = "-".join(value.strip().lower().split())
    if not _SLUG_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Event slug must contain lowercase letters, digits, and hyphens"
        )
    return normalized


def normalize_event_target(target_type: str, value: str) -> str:
    if target_type not in EVENT_TARGET_TYPES:
        raise ValueError(f"Unsupported event target type: {target_type}")
    stripped = value.strip()
    if target_type == "uid":
        if not stripped.isdecimal() or int(stripped) <= 0:
            raise ValueError("UID target must be a positive decimal integer")
        return str(int(stripped))
    if target_type == "seed_bvid":
        if not _BVID_PATTERN.fullmatch(stripped):
            raise ValueError("Seed BVID must match the canonical BV format")
        return stripped
    normalized = " ".join(stripped.split()).casefold()
    if not normalized:
        raise ValueError(f"{target_type} target cannot be empty")
    return normalized


def validate_event_window(
    start_at: datetime | None,
    end_at: datetime | None,
) -> None:
    if start_at is not None and end_at is not None and end_at < start_at:
        raise ValueError("Event end_at cannot be before start_at")


def validate_event_status(status: str) -> str:
    if status not in EVENT_STATUSES:
        raise ValueError(f"Unsupported event status: {status}")
    return status


def normalize_event_timezone(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Event timezone cannot be empty")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown event timezone: {normalized}") from exc
    return normalized
