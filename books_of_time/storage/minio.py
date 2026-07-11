from __future__ import annotations

import hashlib
import io
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import zstandard

from books_of_time.storage.base import StoredRawPayload


class MinioRawPayloadStore:
    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str = "raw",
        create_bucket: bool = False,
    ) -> None:
        if not bucket.strip():
            raise ValueError("MinIO raw storage bucket cannot be empty")
        self.client = client
        self.bucket = bucket.strip()
        self.prefix = _normalize_prefix(prefix)
        self.create_bucket = create_bucket
        self._bucket_ready = False

    def save(
        self,
        *,
        body: bytes,
        captured_at: datetime,
        run_id: str,
        suffix: str,
    ) -> StoredRawPayload:
        self._ensure_bucket()
        payload_hash = hashlib.sha256(body).hexdigest()
        compressed = zstandard.ZstdCompressor().compress(body)
        filename = f"{payload_hash}{suffix}.zst"
        parts = [
            self.prefix,
            f"{captured_at:%Y}",
            f"{captured_at:%m}",
            f"{captured_at:%d}",
            run_id,
            filename,
        ]
        object_name = "/".join(part for part in parts if part)
        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(compressed),
            len(compressed),
            content_type="application/zstd",
            metadata={
                "payload-sha256": payload_hash,
                "uncompressed-size": str(len(body)),
            },
        )
        return StoredRawPayload(
            storage_uri=f"s3://{self.bucket}/{object_name}",
            payload_hash_hex=payload_hash,
            compressed_size=len(compressed),
            uncompressed_size=len(body),
        )

    def read_uri(self, storage_uri: str) -> bytes:
        parsed = urlparse(storage_uri)
        if parsed.scheme != "s3" or parsed.netloc != self.bucket:
            raise ValueError(f"Unsupported raw payload storage URI: {storage_uri}")
        object_name = parsed.path.lstrip("/")
        if not object_name:
            raise ValueError(f"Raw payload object name is missing: {storage_uri}")
        response = self.client.get_object(self.bucket, object_name)
        try:
            compressed = response.read()
        finally:
            response.close()
            response.release_conn()
        return zstandard.ZstdDecompressor().decompress(compressed)

    def probe(self) -> str:
        self._ensure_bucket()
        return f"reachable bucket: {self.bucket}"

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        if self.client.bucket_exists(self.bucket):
            self._bucket_ready = True
            return
        if not self.create_bucket:
            raise RuntimeError(f"MinIO bucket does not exist: {self.bucket}")
        self.client.make_bucket(self.bucket)
        self._bucket_ready = True


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip().strip("/")
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise ValueError("MinIO prefix cannot contain relative path segments")
    return normalized
