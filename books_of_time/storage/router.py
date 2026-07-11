from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from books_of_time.storage.base import RawPayloadStore, StoredRawPayload


class RawPayloadStoreRouter:
    def __init__(
        self,
        *,
        primary: RawPayloadStore,
        readers: dict[str, RawPayloadStore],
    ) -> None:
        self.primary = primary
        self.readers = dict(readers)

    def save(
        self,
        *,
        body: bytes,
        captured_at: datetime,
        run_id: str,
        suffix: str,
    ) -> StoredRawPayload:
        return self.primary.save(
            body=body,
            captured_at=captured_at,
            run_id=run_id,
            suffix=suffix,
        )

    def read_uri(self, storage_uri: str) -> bytes:
        scheme = urlsplit(storage_uri).scheme.casefold()
        reader = self.readers.get(scheme)
        if reader is None:
            raise ValueError(f"Unsupported raw payload storage URI: {storage_uri}")
        return reader.read_uri(storage_uri)

    def probe(self) -> str:
        return self.primary.probe()
