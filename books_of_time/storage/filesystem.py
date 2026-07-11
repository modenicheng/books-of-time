from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import zstandard

from books_of_time.storage.base import StoredRawPayload


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

    def read_uri(self, storage_uri: str) -> bytes:
        if not storage_uri.startswith("file://"):
            raise ValueError(f"Unsupported raw payload storage URI: {storage_uri}")

        path = Path(storage_uri.removeprefix("file://"))
        compressed = path.read_bytes()
        return zstandard.ZstdDecompressor().decompress(compressed)

    def probe(self) -> str:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        probe_path = self.raw_dir / f".books-of-time-raw-{uuid4().hex}"
        try:
            probe_path.write_bytes(b"ok")
        finally:
            probe_path.unlink(missing_ok=True)
        return f"writable: {self.raw_dir}"
