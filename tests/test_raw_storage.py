import hashlib
import io
import json
from datetime import UTC, datetime

import pytest
import zstandard

from books_of_time.storage.factory import build_raw_payload_store
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.storage.minio import MinioRawPayloadStore
from books_of_time.storage.router import RawPayloadStoreRouter


def test_raw_payload_store_writes_zstd_file_under_date_and_run_id(tmp_path) -> None:
    store = RawPayloadFileStore(raw_dir=tmp_path)
    captured_at = datetime(2026, 7, 8, 12, 34, 56, tzinfo=UTC)
    payload = {"code": 0, "data": {"view": 123}}
    body = json.dumps(payload, ensure_ascii=False).encode()

    saved = store.save(
        body=body,
        captured_at=captured_at,
        run_id="run-123",
        suffix=".json",
    )

    expected_hash = hashlib.sha256(body).hexdigest()
    assert saved.payload_hash_hex == expected_hash
    assert saved.uncompressed_size == len(body)
    assert saved.compressed_size > 0
    assert saved.storage_uri == (
        f"file://{tmp_path / '2026' / '07' / '08' / 'run-123' / f'{expected_hash}.json.zst'}"
    )

    compressed = (
        tmp_path / "2026" / "07" / "08" / "run-123" / f"{expected_hash}.json.zst"
    ).read_bytes()
    assert zstandard.ZstdDecompressor().decompress(compressed) == body


def test_raw_payload_file_store_reads_saved_uri(tmp_path) -> None:
    store = RawPayloadFileStore(raw_dir=tmp_path)
    body = b'{"hello":"world"}'
    saved = store.save(
        body=body,
        captured_at=datetime(2099, 1, 1, tzinfo=UTC),
        run_id="run-1",
        suffix=".json",
    )

    assert store.read_uri(saved.storage_uri) == body
    assert "writable" in store.probe()


def test_minio_raw_store_preserves_hash_compression_and_releases_response() -> None:
    client = _FakeMinioClient()
    store = MinioRawPayloadStore(
        client=client,
        bucket="books-raw",
        prefix="archive/raw",
        create_bucket=True,
    )
    body = b'{"hello":"minio"}'
    captured_at = datetime(2026, 7, 11, 2, 3, 4, tzinfo=UTC)

    saved = store.save(
        body=body,
        captured_at=captured_at,
        run_id="run-1",
        suffix=".json",
    )

    digest = hashlib.sha256(body).hexdigest()
    object_name = f"archive/raw/2026/07/11/run-1/{digest}.json.zst"
    assert saved.storage_uri == f"s3://books-raw/{object_name}"
    assert saved.payload_hash_hex == digest
    uploaded = client.objects[("books-raw", object_name)]
    assert zstandard.ZstdDecompressor().decompress(uploaded) == body
    assert client.last_metadata == {
        "payload-sha256": digest,
        "uncompressed-size": str(len(body)),
    }
    assert store.read_uri(saved.storage_uri) == body
    assert client.last_response is not None
    assert client.last_response.closed_by_store is True
    assert client.last_response.released is True
    assert "books-raw" in store.probe()
    assert client.bucket_exists_calls == 1


def test_raw_store_factory_supports_legacy_filesystem_and_minio(tmp_path) -> None:
    filesystem = build_raw_payload_store(
        {"storage": {"backend": "filesystem", "raw_dir": str(tmp_path)}}
    )
    assert isinstance(filesystem, RawPayloadFileStore)

    client = _FakeMinioClient(existing_buckets={"archive"})
    minio = build_raw_payload_store(
        {
            "storage": {
                "backend": "minio",
                "minio": {
                    "bucket": "archive",
                    "prefix": "raw",
                },
            }
        },
        minio_client=client,
    )
    assert isinstance(minio, RawPayloadStoreRouter)
    assert isinstance(minio.primary, MinioRawPayloadStore)
    local_body = b"legacy local raw"
    local = filesystem.save(
        body=local_body,
        captured_at=datetime(2026, 7, 11, tzinfo=UTC),
        run_id="legacy",
        suffix=".json",
    )
    assert minio.read_uri(local.storage_uri) == local_body

    with pytest.raises(ValueError, match="Unsupported raw storage backend"):
        build_raw_payload_store({"storage": {"backend": "unknown"}})


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.closed_by_store = False
        self.released = False

    def close(self) -> None:
        self.closed_by_store = True
        super().close()

    def release_conn(self) -> None:
        self.released = True


class _FakeMinioClient:
    def __init__(self, *, existing_buckets: set[str] | None = None) -> None:
        self.buckets = set(existing_buckets or set())
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_metadata: dict[str, str] | None = None
        self.last_response: _FakeResponse | None = None
        self.bucket_exists_calls = 0

    def bucket_exists(self, bucket_name: str) -> bool:
        self.bucket_exists_calls += 1
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name: str) -> None:
        self.buckets.add(bucket_name)

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data,
        length: int,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        assert content_type == "application/zstd"
        uploaded = data.read()
        assert len(uploaded) == length
        self.objects[(bucket_name, object_name)] = uploaded
        self.last_metadata = metadata

    def get_object(self, bucket_name: str, object_name: str) -> _FakeResponse:
        self.last_response = _FakeResponse(self.objects[(bucket_name, object_name)])
        return self.last_response
