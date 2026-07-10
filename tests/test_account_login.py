from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bilibili_api import login_v2

from books_of_time import cli
from books_of_time.accounts.manager import AccountManager
from books_of_time.accounts.qr_login import QrLoginExpiredError, QrLoginFlow
from books_of_time.accounts.storage import EncryptedFileCredentialStore


class FakeCredential:
    def get_cookies(self) -> dict[str, str]:
        return {
            "SESSDATA": "secret-session",
            "bili_jct": "secret-csrf",
            "DedeUserID": "123456",
            "ac_time_value": "secret-refresh",
        }


class FakeQrLogin:
    def __init__(self, events) -> None:
        self.events = iter(events)
        self.generated = False

    async def generate_qrcode(self) -> None:
        self.generated = True

    def get_qrcode_terminal(self) -> str:
        return "<terminal-qr>"

    async def check_state(self):
        return next(self.events)

    def get_credential(self) -> FakeCredential:
        return FakeCredential()


class FakeRawHttpClient:
    async def request(self, **kwargs):
        raise AssertionError("Fake QR flow should not issue a real request")


def _manager(tmp_path) -> AccountManager:
    return AccountManager(
        store=EncryptedFileCredentialStore(
            credentials_path=tmp_path / "credentials.enc",
            key_path=tmp_path / "master.key",
        ),
        default_account_id="default",
    )


def test_login_parser_has_independent_qr_status_and_logout_commands() -> None:
    qr = cli.build_parser().parse_args(
        ["login", "qr", "--account", "researcher", "--timeout-seconds", "90"]
    )
    assert qr.login_command == "qr"
    assert qr.account == "researcher"
    assert qr.timeout_seconds == 90

    status = cli.build_parser().parse_args(["login", "status"])
    assert status.login_command == "status"
    assert status.account == "default"

    logout = cli.build_parser().parse_args(
        ["login", "logout", "--account", "researcher"]
    )
    assert logout.login_command == "logout"
    assert logout.account == "researcher"


@pytest.mark.asyncio
async def test_qr_login_saves_cookie_without_printing_secrets(tmp_path) -> None:
    manager = _manager(tmp_path)
    output: list[str] = []
    qr = FakeQrLogin(
        [
            login_v2.QrCodeLoginEvents.SCAN,
            login_v2.QrCodeLoginEvents.CONF,
            login_v2.QrCodeLoginEvents.DONE,
        ]
    )
    flow = QrLoginFlow(
        manager=manager,
        http_client=FakeRawHttpClient(),
        rate_limiter=None,
        qr_factory=lambda: qr,
        output=output.append,
        sleep=lambda _: None,
    )

    snapshot = await flow.run(
        account_id="default",
        timeout_seconds=30,
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )

    assert qr.generated is True
    assert manager.store.load_latest("default") == snapshot
    assert snapshot.cookies["SESSDATA"] == "secret-session"
    rendered = "\n".join(output)
    assert "<terminal-qr>" in rendered
    assert "secret-session" not in rendered
    assert "secret-csrf" not in rendered
    assert "secret-refresh" not in rendered


@pytest.mark.asyncio
async def test_expired_qr_does_not_create_credential_store(tmp_path) -> None:
    manager = _manager(tmp_path)
    flow = QrLoginFlow(
        manager=manager,
        http_client=FakeRawHttpClient(),
        rate_limiter=None,
        qr_factory=lambda: FakeQrLogin([login_v2.QrCodeLoginEvents.TIMEOUT]),
        output=lambda _: None,
        sleep=lambda _: None,
    )

    with pytest.raises(QrLoginExpiredError):
        await flow.run(account_id="default", timeout_seconds=30)

    assert manager.store.load_latest("default") is None


def test_account_manager_rejects_incomplete_login_cookie(tmp_path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        manager.save_login(
            account_id="default",
            cookies={"SESSDATA": "only-session"},
            now=datetime(2026, 7, 10, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_login_status_and_logout_helpers_do_not_need_database(tmp_path) -> None:
    cfg = {
        "accounts": {
            "credentials_path": str(tmp_path / "credentials.enc"),
            "key_path": str(tmp_path / "master.key"),
        }
    }
    manager = cli.build_account_manager(cfg)
    manager.save_login(
        account_id="default",
        cookies=FakeCredential().get_cookies(),
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )

    status = await cli._show_login_status(cfg, account_id="default")
    removed = await cli._logout_account(cfg, account_id="default")

    assert status is not None
    assert status.account_id == "default"
    assert removed is True
    assert manager.status("default") is None
