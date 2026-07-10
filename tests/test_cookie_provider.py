from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from books_of_time.accounts.models import CookieHealth
from books_of_time.accounts.provider import CurrentCookieProvider
from books_of_time.accounts.storage import EncryptedFileCredentialStore
from books_of_time.app import build_bilibili_client


def _store(tmp_path) -> EncryptedFileCredentialStore:
    return EncryptedFileCredentialStore(
        credentials_path=tmp_path / "credentials.enc",
        key_path=tmp_path / "master.key",
    )


@pytest.mark.asyncio
async def test_provider_returns_anonymous_for_missing_or_invalid_snapshot(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    provider = CurrentCookieProvider(store=store, default_account_id="default")
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)

    assert await provider.get_cookies() == {}
    store.save_snapshot(
        account_id="default",
        cookies={"SESSDATA": "session-1", "bili_jct": "csrf-1"},
        source="qr_login",
        now=now,
    )
    assert await provider.get_cookies() == {
        "SESSDATA": "session-1",
        "bili_jct": "csrf-1",
    }

    store.mark_health(
        account_id="default",
        health=CookieHealth.INVALID,
        checked_at=now + timedelta(minutes=1),
    )
    assert await provider.get_cookies() == {}


@pytest.mark.asyncio
async def test_provider_observes_external_snapshot_rotation_without_restart(
    tmp_path,
) -> None:
    service_store = _store(tmp_path)
    login_store = _store(tmp_path)
    provider = CurrentCookieProvider(
        store=service_store,
        default_account_id="default",
    )
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)

    login_store.save_snapshot(
        account_id="default",
        cookies={"SESSDATA": "session-1"},
        source="qr_login",
        now=now,
    )
    assert (await provider.get_cookies())["SESSDATA"] == "session-1"

    login_store.save_snapshot(
        account_id="default",
        cookies={"SESSDATA": "session-2"},
        source="refresh",
        now=now + timedelta(minutes=1),
    )
    assert (await provider.get_cookies())["SESSDATA"] == "session-2"


@pytest.mark.asyncio
async def test_provider_can_select_explicit_future_account_id(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    store.save_snapshot(
        account_id="researcher-b",
        cookies={"SESSDATA": "session-b"},
        source="qr_login",
        now=now,
    )
    provider = CurrentCookieProvider(store=store, default_account_id="default")

    assert await provider.get_cookies() == {}
    assert await provider.get_cookies("researcher-b") == {"SESSDATA": "session-b"}


def test_application_client_wires_provider_without_creating_account_files(
    tmp_path,
) -> None:
    credentials_path = tmp_path / "credentials.enc"
    key_path = tmp_path / "master.key"
    client = build_bilibili_client(
        {
            "accounts": {
                "enabled": True,
                "active_account_id": "default",
                "credentials_path": str(credentials_path),
                "key_path": str(key_path),
            }
        }
    )

    assert isinstance(client.http_client.cookie_provider, CurrentCookieProvider)
    assert not credentials_path.exists()
    assert not key_path.exists()


def test_application_client_can_disable_managed_accounts() -> None:
    client = build_bilibili_client({"accounts": {"enabled": False}})
    assert client.http_client.cookie_provider is None
