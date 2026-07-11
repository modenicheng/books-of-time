from __future__ import annotations

from typing import Any

from books_of_time.storage.base import RawPayloadStore
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.storage.minio import MinioRawPayloadStore
from books_of_time.storage.router import RawPayloadStoreRouter


def build_raw_payload_store(
    cfg: dict[str, Any],
    *,
    minio_client: Any | None = None,
) -> RawPayloadStore:
    storage = cfg.get("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("Configuration section storage must be a mapping")
    backend = str(storage.get("backend", "filesystem")).strip().casefold()
    if backend == "filesystem":
        return RawPayloadFileStore(storage.get("raw_dir", "./data/raw"))
    if backend != "minio":
        raise ValueError(f"Unsupported raw storage backend: {backend}")

    minio_cfg = storage.get("minio", {})
    if not isinstance(minio_cfg, dict):
        raise ValueError("Configuration section storage.minio must be a mapping")
    client = minio_client or _build_minio_client(minio_cfg)
    minio_store = MinioRawPayloadStore(
        client=client,
        bucket=str(minio_cfg.get("bucket", "books-of-time-raw")),
        prefix=str(minio_cfg.get("prefix", "raw")),
        create_bucket=bool(minio_cfg.get("create_bucket", False)),
    )
    filesystem_store = RawPayloadFileStore(storage.get("raw_dir", "./data/raw"))
    return RawPayloadStoreRouter(
        primary=minio_store,
        readers={"file": filesystem_store, "s3": minio_store},
    )


def _build_minio_client(cfg: dict[str, Any]) -> Any:
    from minio import Minio

    endpoint = str(cfg.get("endpoint", "")).strip()
    access_key = str(cfg.get("access_key", "")).strip()
    secret_key = str(cfg.get("secret_key", "")).strip()
    if not endpoint or not access_key or not secret_key:
        raise ValueError(
            "MinIO raw storage requires endpoint, access_key, and secret_key"
        )
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=bool(cfg.get("secure", True)),
    )
