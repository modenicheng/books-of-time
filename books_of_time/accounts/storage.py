from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock

from books_of_time.accounts.models import CookieHealth, CredentialSnapshot

_FORMAT_VERSION = 1
_ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class CredentialStoreError(RuntimeError):
    pass


class EncryptedFileCredentialStore:
    def __init__(
        self,
        *,
        credentials_path: str | Path,
        key_path: str | Path,
        history_limit: int = 5,
        lock_timeout_seconds: float = 10,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.key_path = Path(key_path)
        self.history_limit = max(int(history_limit), 1)
        self.lock_timeout_seconds = max(float(lock_timeout_seconds), 0)
        self.lock_path = self.credentials_path.with_name(
            f"{self.credentials_path.name}.lock"
        )

    def load_latest(self, account_id: str) -> CredentialSnapshot | None:
        self._validate_account_id(account_id)
        with self._lock():
            document = self._read_document_unlocked()
        account = document["accounts"].get(account_id)
        if account is None:
            return None
        active_id = account.get("active_snapshot_id")
        for record in reversed(account.get("snapshots", [])):
            if record.get("snapshot_id") == active_id:
                return self._snapshot_from_record(account_id, record)
        raise CredentialStoreError(
            f"Active credential snapshot is missing for account {account_id}"
        )

    def list_snapshots(self, account_id: str) -> list[CredentialSnapshot]:
        self._validate_account_id(account_id)
        with self._lock():
            document = self._read_document_unlocked()
        account = document["accounts"].get(account_id)
        if account is None:
            return []
        return [
            self._snapshot_from_record(account_id, record)
            for record in account.get("snapshots", [])
        ]

    def save_snapshot(
        self,
        *,
        account_id: str,
        cookies: Mapping[str, str],
        source: str,
        now: datetime,
        health: CookieHealth = CookieHealth.VALID,
    ) -> CredentialSnapshot:
        self._validate_account_id(account_id)
        self._validate_datetime(now, "now")
        normalized_source = source.strip()
        if not normalized_source:
            raise ValueError("Credential snapshot source cannot be empty")
        normalized_cookies = {
            str(key): str(value)
            for key, value in cookies.items()
            if str(key) and value is not None and str(value)
        }
        if not normalized_cookies:
            raise ValueError("Credential snapshot cookies cannot be empty")

        snapshot = CredentialSnapshot(
            snapshot_id=uuid4().hex,
            account_id=account_id,
            source=normalized_source,
            created_at=now.astimezone(UTC),
            health=health,
            last_checked_at=now.astimezone(UTC),
            cookies=normalized_cookies,
        )
        with self._lock():
            document = self._read_document_unlocked()
            account = document["accounts"].setdefault(
                account_id,
                {"active_snapshot_id": None, "snapshots": []},
            )
            for record in account["snapshots"]:
                if record["snapshot_id"] == account.get("active_snapshot_id"):
                    record["health"] = CookieHealth.SUPERSEDED.value
            account["snapshots"].append(self._snapshot_to_record(snapshot))
            account["snapshots"] = account["snapshots"][-self.history_limit :]
            account["active_snapshot_id"] = snapshot.snapshot_id
            self._write_document_unlocked(document)
        return snapshot

    def mark_health(
        self,
        *,
        account_id: str,
        health: CookieHealth,
        checked_at: datetime,
    ) -> CredentialSnapshot | None:
        self._validate_account_id(account_id)
        self._validate_datetime(checked_at, "checked_at")
        with self._lock():
            document = self._read_document_unlocked()
            account = document["accounts"].get(account_id)
            if account is None:
                return None
            active_id = account.get("active_snapshot_id")
            active_record = next(
                (
                    record
                    for record in account.get("snapshots", [])
                    if record.get("snapshot_id") == active_id
                ),
                None,
            )
            if active_record is None:
                raise CredentialStoreError(
                    f"Active credential snapshot is missing for account {account_id}"
                )
            active_record["health"] = health.value
            active_record["last_checked_at"] = self._format_datetime(checked_at)
            self._write_document_unlocked(document)
            return self._snapshot_from_record(account_id, active_record)

    def logout(self, account_id: str) -> bool:
        self._validate_account_id(account_id)
        with self._lock():
            document = self._read_document_unlocked()
            if document["accounts"].pop(account_id, None) is None:
                return False
            self._write_document_unlocked(document)
            return True

    def version_token(self) -> tuple[int, int, int, int] | None:
        try:
            credentials_stat = self.credentials_path.stat()
            key_stat = self.key_path.stat()
        except FileNotFoundError:
            return None
        return (
            credentials_stat.st_mtime_ns,
            credentials_stat.st_size,
            key_stat.st_mtime_ns,
            key_stat.st_size,
        )

    def _read_document_unlocked(self) -> dict[str, Any]:
        credentials_exists = self.credentials_path.exists()
        key_exists = self.key_path.exists()
        if not credentials_exists:
            return self._empty_document()
        if not key_exists:
            raise CredentialStoreError(
                "Encrypted credential store exists but its key is missing"
            )
        try:
            key = self.key_path.read_bytes().strip()
            plaintext = Fernet(key).decrypt(self.credentials_path.read_bytes())
            document = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("Credential store cannot be decrypted") from exc
        if not isinstance(document, dict) or document.get("format_version") != 1:
            raise CredentialStoreError("Unsupported credential store format")
        if not isinstance(document.get("accounts"), dict):
            raise CredentialStoreError("Credential store accounts must be a mapping")
        return document

    def _write_document_unlocked(self, document: dict[str, Any]) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = self._load_or_create_key_unlocked()
        plaintext = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = Fernet(key).encrypt(plaintext)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.credentials_path.parent,
                prefix=f".{self.credentials_path.name}.",
                delete=False,
            ) as temp_file:
                temp_file.write(encrypted)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            self._restrict_permissions(temp_path)
            os.replace(temp_path, self.credentials_path)
            self._restrict_permissions(self.credentials_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _load_or_create_key_unlocked(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.write_bytes(key + b"\n")
        self._restrict_permissions(self.key_path)
        return key

    def _lock(self) -> FileLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.lock_path), timeout=self.lock_timeout_seconds)

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {"format_version": _FORMAT_VERSION, "accounts": {}}

    @staticmethod
    def _snapshot_to_record(snapshot: CredentialSnapshot) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "source": snapshot.source,
            "created_at": snapshot.created_at.isoformat(),
            "health": snapshot.health.value,
            "last_checked_at": snapshot.last_checked_at.isoformat()
            if snapshot.last_checked_at is not None
            else None,
            "cookies": dict(snapshot.cookies),
        }

    @staticmethod
    def _snapshot_from_record(
        account_id: str,
        record: Mapping[str, Any],
    ) -> CredentialSnapshot:
        try:
            return CredentialSnapshot(
                snapshot_id=str(record["snapshot_id"]),
                account_id=account_id,
                source=str(record["source"]),
                created_at=datetime.fromisoformat(str(record["created_at"])),
                health=CookieHealth(str(record["health"])),
                last_checked_at=datetime.fromisoformat(str(record["last_checked_at"]))
                if record.get("last_checked_at") is not None
                else None,
                cookies={
                    str(key): str(value)
                    for key, value in dict(record["cookies"]).items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialStoreError("Credential snapshot record is invalid") from exc

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _validate_datetime(value: datetime, field_name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")

    @staticmethod
    def _validate_account_id(account_id: str) -> None:
        if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise ValueError(
                "account_id must contain 1-64 letters, digits, dots, underscores, or hyphens"
            )

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass
