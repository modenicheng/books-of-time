from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StoredRawPayload:
    storage_uri: str
    payload_hash_hex: str
    compressed_size: int
    uncompressed_size: int


class RawPayloadStore(Protocol):
    def save(
        self,
        *,
        body: bytes,
        captured_at: datetime,
        run_id: str,
        suffix: str,
    ) -> StoredRawPayload: ...

    def read_uri(self, storage_uri: str) -> bytes: ...

    def probe(self) -> str: ...
