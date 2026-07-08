from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import zstandard


@dataclass(frozen=True)
class StoredRawPayload:
    storage_uri: str
    payload_hash_hex: str
    compressed_size: int
    uncompressed_size: int


class RawPayloadFileStore:
    def __init__(self, raw_dir: str | Path) -> None:
        self.raw_dir = Path(raw_dir)

    def save(
        self,
        *,
        body: bytes,
        captured_at: datetime,
        run_id: str,
        suffix: str,
    ) -> StoredRawPayload:
        payload_hash = hashlib.sha256(body).hexdigest()
        relative_dir = (
            Path(f"{captured_at:%Y}")
            / f"{captured_at:%m}"
            / f"{captured_at:%d}"
            / run_id
        )
        target_dir = self.raw_dir / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{payload_hash}{suffix}.zst"
        target_path = target_dir / filename
        compressed = zstandard.ZstdCompressor().compress(body)
        target_path.write_bytes(compressed)

        return StoredRawPayload(
            storage_uri=f"file://{target_path}",
            payload_hash_hex=payload_hash,
            compressed_size=len(compressed),
            uncompressed_size=len(body),
        )
