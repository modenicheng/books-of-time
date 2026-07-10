from __future__ import annotations

from datetime import UTC, datetime

import pytest

from books_of_time.accounts.models import AccountRefreshResult, RefreshAction
from books_of_time.domain.enums import ScheduledJobKind
from books_of_time.service.scheduled_jobs import (
    AccountCookieRefreshScheduleHandler,
    build_default_scheduled_jobs,
)


class FakeAccountManager:
    def __init__(self) -> None:
        self.calls = []

    async def refresh_if_needed(self, **kwargs) -> AccountRefreshResult:
        self.calls.append(kwargs)
        return AccountRefreshResult(
            account_id=kwargs.get("account_id") or "default",
            action=RefreshAction.UNCHANGED,
            previous_snapshot_id="snapshot-1",
            current_snapshot_id="snapshot-1",
        )


class FakeBilibiliClient:
    def __init__(self) -> None:
        self.http_client = object()
        self.rate_limiter = object()


@pytest.mark.asyncio
async def test_cookie_refresh_schedule_handler_delegates_without_database_state() -> (
    None
):
    manager = FakeAccountManager()
    client = FakeBilibiliClient()
    handler = AccountCookieRefreshScheduleHandler(
        manager=manager,
        http_client=client.http_client,
        rate_limiter=client.rate_limiter,
        account_id="default",
    )
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)

    await handler.handle(None, None, now=now)

    assert manager.calls == [
        {
            "http_client": client.http_client,
            "rate_limiter": client.rate_limiter,
            "account_id": "default",
            "now": now,
        }
    ]


def test_default_scheduled_jobs_include_cookie_refresh_when_wired() -> None:
    manager = FakeAccountManager()
    client = FakeBilibiliClient()

    definitions, handlers = build_default_scheduled_jobs(
        {
            "accounts": {
                "enabled": True,
                "active_account_id": "default",
                "auto_refresh": True,
                "refresh_check_seconds": 3600,
            },
            "discovery": {"matrix_uids": []},
        },
        account_manager=manager,
        bilibili_client=client,
    )

    definition = next(
        item
        for item in definitions
        if item.job_kind == ScheduledJobKind.ACCOUNT_COOKIE_REFRESH
    )
    assert definition.job_key == "account-cookie-refresh:default"
    assert definition.schedule_seconds == 3600
    assert ScheduledJobKind.ACCOUNT_COOKIE_REFRESH in handlers


def test_cookie_refresh_job_can_be_disabled() -> None:
    definitions, handlers = build_default_scheduled_jobs(
        {
            "accounts": {"enabled": True, "auto_refresh": False},
            "discovery": {"matrix_uids": []},
        },
        account_manager=FakeAccountManager(),
        bilibili_client=FakeBilibiliClient(),
    )

    assert ScheduledJobKind.ACCOUNT_COOKIE_REFRESH not in {
        item.job_kind for item in definitions
    }
    assert ScheduledJobKind.ACCOUNT_COOKIE_REFRESH not in handlers
