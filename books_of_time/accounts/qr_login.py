from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from bilibili_api import login_v2

from books_of_time.accounts.manager import AccountManager
from books_of_time.accounts.models import CredentialSnapshot
from books_of_time.http.client import RawHttpClient
from books_of_time.http.rate_limiter import TokenBucketRateLimiter
from books_of_time.platforms.bilibili.request_client import (
    capture_bili_api_requests,
)


class QrLoginError(RuntimeError):
    pass


class QrLoginExpiredError(QrLoginError):
    pass


class QrLoginTimeoutError(QrLoginError):
    pass


class QrLoginClient(Protocol):
    async def generate_qrcode(self) -> None: ...

    def get_qrcode_terminal(self) -> str: ...

    async def check_state(self) -> login_v2.QrCodeLoginEvents: ...

    def get_credential(self): ...


class QrLoginFlow:
    def __init__(
        self,
        *,
        manager: AccountManager,
        http_client: RawHttpClient,
        rate_limiter: TokenBucketRateLimiter | None,
        qr_factory: Callable[[], QrLoginClient] = login_v2.QrCodeLogin,
        output: Callable[[str], None] = print,
        sleep: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
        poll_seconds: float = 2,
    ) -> None:
        self.manager = manager
        self.http_client = http_client
        self.rate_limiter = rate_limiter
        self.qr_factory = qr_factory
        self.output = output
        self.sleep = sleep
        self.poll_seconds = max(float(poll_seconds), 0)

    async def run(
        self,
        *,
        account_id: str | None = None,
        timeout_seconds: float = 180,
        now: datetime | None = None,
    ) -> CredentialSnapshot:
        if timeout_seconds <= 0:
            raise ValueError("QR login timeout_seconds must be positive")
        qr = self.qr_factory()
        try:
            async with asyncio.timeout(timeout_seconds):
                with capture_bili_api_requests(
                    http_client=self.http_client,
                    rate_limiter=self.rate_limiter,
                    use_managed_cookies=False,
                ):
                    await qr.generate_qrcode()
                    self.output(qr.get_qrcode_terminal())
                    self.output("Scan the QR code with the Bilibili mobile app")
                    last_event: login_v2.QrCodeLoginEvents | None = None
                    while True:
                        event = await qr.check_state()
                        if event != last_event:
                            self.output(_event_message(event))
                            last_event = event
                        if event == login_v2.QrCodeLoginEvents.DONE:
                            credential = qr.get_credential()
                            snapshot = self.manager.save_login(
                                account_id=account_id,
                                cookies=credential.get_cookies(),
                                now=now or datetime.now(UTC),
                            )
                            self.output(
                                "Login succeeded "
                                f"account={snapshot.account_id} "
                                f"snapshot={snapshot.snapshot_id}"
                            )
                            return snapshot
                        if event == login_v2.QrCodeLoginEvents.TIMEOUT:
                            raise QrLoginExpiredError("Bilibili QR code expired")
                        maybe_awaitable = self.sleep(self.poll_seconds)
                        if inspect.isawaitable(maybe_awaitable):
                            await maybe_awaitable
        except TimeoutError as exc:
            raise QrLoginTimeoutError("Bilibili QR login timed out") from exc


def _event_message(event: login_v2.QrCodeLoginEvents) -> str:
    messages = {
        login_v2.QrCodeLoginEvents.SCAN: "Waiting for QR scan",
        login_v2.QrCodeLoginEvents.CONF: "QR scanned; confirm login on mobile",
        login_v2.QrCodeLoginEvents.TIMEOUT: "QR code expired",
        login_v2.QrCodeLoginEvents.DONE: "Bilibili confirmed login",
    }
    return messages[event]
