from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from books_of_time.accounts.manager import AccountManager
from books_of_time.accounts.models import CookieHealth, RefreshAction
from books_of_time.accounts.storage import EncryptedFileCredentialStore


class FakeRawHttpClient:
    async def request(self, **kwargs):
        raise AssertionError("Fake credential should not issue a real request")


class FakeCredential:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        valid: bool = True,
        needs_refresh: bool = False,
        refreshed_cookies: dict[str, str] | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        self.cookies = dict(cookies)
        self.valid = valid
        self.needs_refresh = needs_refresh
        self.refreshed_cookies = refreshed_cookies
        self.refresh_error = refresh_error
        self.check_refresh_calls = 0

    async def check_valid(self) -> bool:
        return self.valid

    async def check_refresh(self) -> bool:
        self.check_refresh_calls += 1
        return self.needs_refresh

    async def refresh(self) -> None:
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refreshed_cookies is not None:
            self.cookies = dict(self.refreshed_cookies)

    def get_cookies(self) -> dict[str, str]:
        return dict(self.cookies)


def _cookies(version: int) -> dict[str, str]:
    return {
        "SESSDATA": f"session-{version}",
        "bili_jct": f"csrf-{version}",
        "DedeUserID": "123456",
        "ac_time_value": f"refresh-{version}",
    }


def _store(tmp_path) -> EncryptedFileCredentialStore:
    return EncryptedFileCredentialStore(
        credentials_path=tmp_path / "credentials.enc",
        key_path=tmp_path / "master.key",
    )


@pytest.mark.asyncio
async def test_refresh_without_snapshot_is_anonymous_and_skips_network(
    tmp_path,
) -> None:
    factory_calls = []
    manager = AccountManager(
        store=_store(tmp_path),
        credential_factory=lambda cookies: factory_calls.append(cookies),
    )

    result = await manager.refresh_if_needed(
        http_client=FakeRawHttpClient(),
        rate_limiter=None,
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )

    assert result.action == RefreshAction.ANONYMOUS
    assert factory_calls == []


@pytest.mark.asyncio
async def test_invalid_cookie_is_marked_and_future_requests_can_fall_back(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=now,
    )
    credential = FakeCredential(_cookies(1), valid=False)
    manager = AccountManager(
        store=store,
        credential_factory=lambda cookies: credential,
    )

    result = await manager.refresh_if_needed(
        http_client=FakeRawHttpClient(),
        rate_limiter=None,
        now=now + timedelta(hours=1),
    )

    assert result.action == RefreshAction.INVALID
    assert credential.check_refresh_calls == 0
    assert store.load_latest("default").health == CookieHealth.INVALID


@pytest.mark.asyncio
async def test_valid_cookie_without_refresh_updates_check_time_only(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    original = store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=now,
    )
    checked_at = now + timedelta(hours=1)
    manager = AccountManager(
        store=store,
        credential_factory=lambda cookies: FakeCredential(cookies),
    )

    result = await manager.refresh_if_needed(
        http_client=FakeRawHttpClient(),
        rate_limiter=None,
        now=checked_at,
    )

    latest = store.load_latest("default")
    assert result.action == RefreshAction.UNCHANGED
    assert latest.snapshot_id == original.snapshot_id
    assert latest.health == CookieHealth.VALID
    assert latest.last_checked_at == checked_at


@pytest.mark.asyncio
async def test_refresh_rotates_to_new_cookie_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    original = store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=now,
    )
    manager = AccountManager(
        store=store,
        credential_factory=lambda cookies: FakeCredential(
            cookies,
            needs_refresh=True,
            refreshed_cookies=_cookies(2),
        ),
    )

    result = await manager.refresh_if_needed(
        http_client=FakeRawHttpClient(),
        rate_limiter=None,
        now=now + timedelta(hours=1),
    )

    latest = store.load_latest("default")
    assert result.action == RefreshAction.ROTATED
    assert result.previous_snapshot_id == original.snapshot_id
    assert result.current_snapshot_id == latest.snapshot_id
    assert latest.snapshot_id != original.snapshot_id
    assert latest.source == "refresh"
    assert latest.cookies == _cookies(2)


@pytest.mark.asyncio
async def test_transient_refresh_failure_preserves_previous_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    original = store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=now,
    )
    manager = AccountManager(
        store=store,
        credential_factory=lambda cookies: FakeCredential(
            cookies,
            needs_refresh=True,
            refresh_error=RuntimeError("temporary refresh failure"),
        ),
    )

    with pytest.raises(RuntimeError, match="temporary"):
        await manager.refresh_if_needed(
            http_client=FakeRawHttpClient(),
            rate_limiter=None,
            now=now + timedelta(hours=1),
        )

    latest = store.load_latest("default")
    assert latest.snapshot_id == original.snapshot_id
    assert latest.cookies == original.cookies
