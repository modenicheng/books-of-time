from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from books_of_time.accounts.models import AccountStatus, CredentialSnapshot
from books_of_time.accounts.storage import EncryptedFileCredentialStore

_REQUIRED_LOGIN_COOKIES = frozenset(
    {"SESSDATA", "bili_jct", "DedeUserID", "ac_time_value"}
)


class AccountManager:
    def __init__(
        self,
        *,
        store: EncryptedFileCredentialStore,
        default_account_id: str = "default",
    ) -> None:
        self.store = store
        self.default_account_id = default_account_id

    def save_login(
        self,
        *,
        cookies: Mapping[str, str],
        now: datetime,
        account_id: str | None = None,
    ) -> CredentialSnapshot:
        missing = sorted(key for key in _REQUIRED_LOGIN_COOKIES if not cookies.get(key))
        if missing:
            raise ValueError(
                f"Login credential is missing required Cookie fields: {', '.join(missing)}"
            )
        return self.store.save_snapshot(
            account_id=account_id or self.default_account_id,
            cookies=cookies,
            source="qr_login",
            now=now,
        )

    def status(self, account_id: str | None = None) -> AccountStatus | None:
        snapshot = self.store.load_latest(account_id or self.default_account_id)
        return snapshot.status() if snapshot is not None else None

    def logout(self, account_id: str | None = None) -> bool:
        return self.store.logout(account_id or self.default_account_id)
