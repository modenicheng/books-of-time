from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from books_of_time.accounts.models import CookieHealth, CredentialSnapshot
from books_of_time.accounts.storage import EncryptedFileCredentialStore


class CookieProvider(Protocol):
    async def get_cookies(
        self,
        account_id: str | None = None,
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class _CachedSnapshot:
    version_token: tuple[int, int, int, int] | None
    snapshot: CredentialSnapshot | None


class CurrentCookieProvider:
    def __init__(
        self,
        *,
        store: EncryptedFileCredentialStore,
        default_account_id: str = "default",
    ) -> None:
        self.store = store
        self.default_account_id = default_account_id
        self._cache: dict[str, _CachedSnapshot] = {}

    async def get_cookies(
        self,
        account_id: str | None = None,
    ) -> dict[str, str]:
        effective_account_id = account_id or self.default_account_id
        version_token = self.store.version_token()
        cached = self._cache.get(effective_account_id)
        if cached is None or cached.version_token != version_token:
            cached = _CachedSnapshot(
                version_token=version_token,
                snapshot=self.store.load_latest(effective_account_id),
            )
            self._cache[effective_account_id] = cached

        snapshot = cached.snapshot
        if snapshot is None or snapshot.health == CookieHealth.INVALID:
            return {}
        return {key: value for key, value in snapshot.cookies.items() if key and value}
