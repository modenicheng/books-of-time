from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DISCOVERY_FOCUS_TIMES = (
    "11:00",
    "12:00",
    "13:00",
    "18:00",
    "19:00",
    "19:30",
    "20:00",
)

_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


@dataclass(frozen=True)
class DiscoverySchedulePolicy:
    start_hour: int = 10
    stop_hour: int = 22
    timezone_name: str = "Asia/Shanghai"
    focus_times: tuple[str, ...] = DEFAULT_DISCOVERY_FOCUS_TIMES

    def __post_init__(self) -> None:
        if not 0 <= self.start_hour < self.stop_hour <= 24:
            raise ValueError(
                "Discovery hours must satisfy 0 <= start_hour < stop_hour <= 24"
            )
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Unknown discovery timezone: {self.timezone_name}"
            ) from exc

        normalized_focus_times = tuple(str(value) for value in self.focus_times)
        if len(set(normalized_focus_times)) != len(normalized_focus_times):
            raise ValueError("Discovery focus times must be unique")
        for value in normalized_focus_times:
            if _TIME_PATTERN.fullmatch(value) is None:
                raise ValueError(
                    f"Discovery focus time must use 24-hour HH:MM syntax: {value}"
                )
            hour = int(value[:2])
            if not self.start_hour <= hour < self.stop_hour:
                raise ValueError(
                    f"Discovery focus time must fall inside the active window: {value}"
                )
        object.__setattr__(self, "focus_times", normalized_focus_times)

    def allows_discovery(self, at: datetime) -> bool:
        local = self._to_local(at)
        return self.start_hour <= local.hour < self.stop_hour

    def focus_time_for(self, at: datetime) -> str | None:
        slot = self.focus_slot_for(at)
        if slot is None:
            return None
        return f"{slot.hour:02d}:{slot.minute:02d}"

    def focus_slot_for(self, at: datetime) -> datetime | None:
        local = self._to_local(at)
        if not self.start_hour <= local.hour < self.stop_hour:
            return None
        label = f"{local.hour:02d}:{local.minute:02d}"
        if label not in self.focus_times:
            return None
        return local.replace(second=0, microsecond=0)

    def _to_local(self, at: datetime) -> datetime:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Discovery schedule timestamps must be timezone-aware")
        return at.astimezone(ZoneInfo(self.timezone_name))
