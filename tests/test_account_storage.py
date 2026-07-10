from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from books_of_time.accounts.models import CookieHealth
from books_of_time.accounts.storage import EncryptedFileCredentialStore


def _cookies(version: int) -> dict[str, str]:
    return {
        "SESSDATA": f"secret-session-{version}",
        "bili_jct": f"csrf-{version}",
        "DedeUserID": "123456",
        "ac_time_value": f"refresh-{version}",
    }


def _store(tmp_path, *, history_limit: int = 3) -> EncryptedFileCredentialStore:
    return EncryptedFileCredentialStore(
        credentials_path=tmp_path / "credentials.enc",
        key_path=tmp_path / "master.key",
        history_limit=history_limit,
    )


def test_missing_store_is_anonymous_and_does_not_create_files(tmp_path) -> None:
    store = _store(tmp_path)

    assert store.load_latest("default") is None
    assert not (tmp_path / "credentials.enc").exists()
    assert not (tmp_path / "master.key").exists()


def test_key_left_by_interrupted_first_write_can_be_reused(tmp_path) -> None:
    (tmp_path / "master.key").write_bytes(Fernet.generate_key())
    store = _store(tmp_path)

    assert store.load_latest("default") is None
    saved = store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )
    assert store.load_latest("default") == saved


def test_credential_round_trip_is_encrypted_and_repr_redacts_cookies(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)

    saved = store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=now,
    )
    loaded = store.load_latest("default")

    assert loaded == saved
    assert loaded is not None
    assert loaded.cookies == _cookies(1)
    assert loaded.health == CookieHealth.VALID
    assert "secret-session-1" not in repr(loaded)
    assert b"secret-session-1" not in (tmp_path / "credentials.enc").read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "master.key").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "credentials.enc").stat().st_mode) == 0o600


def test_new_snapshots_rotate_active_version_and_bound_history(tmp_path) -> None:
    store = _store(tmp_path, history_limit=2)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)

    snapshots = [
        store.save_snapshot(
            account_id="default",
            cookies=_cookies(version),
            source="refresh" if version > 1 else "qr_login",
            now=now + timedelta(minutes=version),
        )
        for version in range(1, 4)
    ]

    latest = store.load_latest("default")
    history = store.list_snapshots("default")
    assert latest is not None
    assert latest.snapshot_id == snapshots[-1].snapshot_id
    assert latest.cookies == _cookies(3)
    assert [item.snapshot_id for item in history] == [
        snapshots[-2].snapshot_id,
        snapshots[-1].snapshot_id,
    ]


def test_health_update_and_logout_do_not_expose_or_retain_active_cookie(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    saved = store.save_snapshot(
        account_id="default",
        cookies=_cookies(1),
        source="qr_login",
        now=now,
    )

    updated = store.mark_health(
        account_id="default",
        health=CookieHealth.INVALID,
        checked_at=now + timedelta(hours=1),
    )
    assert updated is not None
    assert updated.snapshot_id == saved.snapshot_id
    assert updated.health == CookieHealth.INVALID
    assert updated.last_checked_at == now + timedelta(hours=1)

    assert store.logout("default") is True
    assert store.load_latest("default") is None
    assert store.logout("default") is False


@pytest.mark.parametrize("account_id", ["", "contains space", "../escape"])
def test_store_rejects_unsafe_account_ids(tmp_path, account_id: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="account_id"):
        store.load_latest(account_id)
