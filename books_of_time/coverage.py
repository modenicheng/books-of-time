from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from books_of_time.domain.enums import TaskKind


@dataclass(slots=True)
class CoverageDraft:
    task_kind: TaskKind
    target_type: str
    target_id: str
    pages_requested: int = 0
    pages_succeeded: int = 0
    items_observed: int = 0
    raw_payloads_saved: int = 0
    parse_errors: int = 0
    request_errors: int = 0
    frontier_reached: bool | None = None
    frontier_missing: bool | None = None
    truncated: bool = False
    corrupted: bool = False
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.corrupted:
            return "corrupted"
        if self.reason in {"time_budget", "frontier_missing"} or self.truncated:
            return "partial"
        return "succeeded"


@dataclass(frozen=True, slots=True)
class EventCoverageSummary:
    event_id: int
    event_slug: str
    active_video_count: int
    videos_with_coverage: int
    coverage_row_count: int
    succeeded_count: int
    partial_count: int
    failed_count: int
    pages_requested: int
    pages_succeeded: int
    items_observed: int
    raw_payloads_saved: int
    parse_errors: int
    request_errors: int
    truncated_count: int
    corrupted_count: int
    first_started_at: datetime | None
    last_finished_at: datetime | None

    @property
    def video_coverage_ratio(self) -> float | None:
        if self.active_video_count == 0:
            return None
        return self.videos_with_coverage / self.active_video_count

    @property
    def page_success_rate(self) -> float | None:
        if self.pages_requested == 0:
            return None
        return self.pages_succeeded / self.pages_requested
