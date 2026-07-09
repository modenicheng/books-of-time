from __future__ import annotations

from dataclasses import dataclass, field
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
