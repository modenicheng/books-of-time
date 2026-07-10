from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ServiceCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ServiceHealthReport:
    checks: tuple[ServiceCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


@dataclass(frozen=True)
class ServiceInstanceSummary:
    instance_id: str
    hostname: str
    pid: int
    version: str
    roles: tuple[str, ...]
    status: str
    started_at: datetime
    heartbeat_at: datetime
    stopped_at: datetime | None
    last_error_type: str | None
    last_error_message: str | None


@dataclass(frozen=True)
class ServiceStatusSnapshot:
    instances: tuple[ServiceInstanceSummary, ...]
    pending_tasks: int
    running_tasks: int
    failed_tasks: int
    oldest_pending_at: datetime | None
    active_backoffs: int
