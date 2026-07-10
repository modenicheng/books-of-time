from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class CookieHealth(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    INVALID = "invalid"
    SUPERSEDED = "superseded"


class RefreshAction(StrEnum):
    ANONYMOUS = "anonymous"
    UNCHANGED = "unchanged"
    INVALID = "invalid"
    ROTATED = "rotated"


@dataclass(frozen=True)
class CredentialSnapshot:
    snapshot_id: str
    account_id: str
    source: str
    created_at: datetime
    health: CookieHealth
    last_checked_at: datetime | None
    cookies: dict[str, str] = field(repr=False)

    def status(self) -> AccountStatus:
        return AccountStatus(
            account_id=self.account_id,
            snapshot_id=self.snapshot_id,
            source=self.source,
            created_at=self.created_at,
            health=self.health,
            last_checked_at=self.last_checked_at,
        )


@dataclass(frozen=True)
class AccountStatus:
    account_id: str
    snapshot_id: str
    source: str
    created_at: datetime
    health: CookieHealth
    last_checked_at: datetime | None


@dataclass(frozen=True)
class AccountRefreshResult:
    account_id: str
    action: RefreshAction
    previous_snapshot_id: str | None
    current_snapshot_id: str | None
