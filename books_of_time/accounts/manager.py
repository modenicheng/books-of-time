from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from bilibili_api.utils.network import Credential

from books_of_time.accounts.models import (
    AccountRefreshResult,
    AccountStatus,
    CookieHealth,
    CredentialSnapshot,
    RefreshAction,
)
from books_of_time.accounts.storage import EncryptedFileCredentialStore

if TYPE_CHECKING:
    from books_of_time.http.client import RawHttpClient
    from books_of_time.http.rate_limiter import RateLimiter

_REQUIRED_LOGIN_COOKIES = frozenset(
    {"SESSDATA", "bili_jct", "DedeUserID", "ac_time_value"}
)


class ManagedCredential(Protocol):
    async def check_valid(self) -> bool: ...

    async def check_refresh(self) -> bool: ...

    async def refresh(self) -> None: ...

    def get_cookies(self) -> dict[str, str]: ...


class AccountManager:
    def __init__(
        self,
        *,
        store: EncryptedFileCredentialStore,
        default_account_id: str = "default",
        credential_factory: Callable[
            [dict[str, str]], ManagedCredential
        ] = Credential.from_cookies,
    ) -> None:
        self.store = store
        self.default_account_id = default_account_id
        self.credential_factory = credential_factory

    def save_login(
        self,
        *,
        cookies: Mapping[str, str],
        now: datetime,
        account_id: str | None = None,
    ) -> CredentialSnapshot:
        self._validate_cookie_fields(cookies)
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

    async def refresh_if_needed(
        self,
        *,
        http_client: RawHttpClient,
        rate_limiter: RateLimiter | None,
        now: datetime,
        account_id: str | None = None,
    ) -> AccountRefreshResult:
        from books_of_time.platforms.bilibili.request_client import (
            capture_bili_api_requests,
        )

        effective_account_id = account_id or self.default_account_id
        snapshot = self.store.load_latest(effective_account_id)
        if snapshot is None:
            return AccountRefreshResult(
                account_id=effective_account_id,
                action=RefreshAction.ANONYMOUS,
                previous_snapshot_id=None,
                current_snapshot_id=None,
            )

        credential = self.credential_factory(dict(snapshot.cookies))
        with capture_bili_api_requests(
            http_client=http_client,
            rate_limiter=rate_limiter,
            use_managed_cookies=False,
        ):
            if not await credential.check_valid():
                self.store.mark_health(
                    account_id=effective_account_id,
                    health=CookieHealth.INVALID,
                    checked_at=now,
                )
                return AccountRefreshResult(
                    account_id=effective_account_id,
                    action=RefreshAction.INVALID,
                    previous_snapshot_id=snapshot.snapshot_id,
                    current_snapshot_id=snapshot.snapshot_id,
                )

            if not await credential.check_refresh():
                self.store.mark_health(
                    account_id=effective_account_id,
                    health=CookieHealth.VALID,
                    checked_at=now,
                )
                return AccountRefreshResult(
                    account_id=effective_account_id,
                    action=RefreshAction.UNCHANGED,
                    previous_snapshot_id=snapshot.snapshot_id,
                    current_snapshot_id=snapshot.snapshot_id,
                )

            await credential.refresh()

        refreshed_cookies = credential.get_cookies()
        self._validate_cookie_fields(refreshed_cookies)
        current = self.store.save_snapshot(
            account_id=effective_account_id,
            cookies=refreshed_cookies,
            source="refresh",
            now=now,
            health=CookieHealth.VALID,
        )
        return AccountRefreshResult(
            account_id=effective_account_id,
            action=RefreshAction.ROTATED,
            previous_snapshot_id=snapshot.snapshot_id,
            current_snapshot_id=current.snapshot_id,
        )

    @staticmethod
    def _validate_cookie_fields(cookies: Mapping[str, str]) -> None:
        missing = sorted(key for key in _REQUIRED_LOGIN_COOKIES if not cookies.get(key))
        if missing:
            raise ValueError(
                "Login credential is missing required Cookie fields: "
                f"{', '.join(missing)}"
            )
