import hashlib
import io
from datetime import UTC, datetime, timedelta

import pytest
import zstandard
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import RawPayload
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.storage.migration import RawPayloadMigrationService
from books_of_time.storage.minio import MinioRawPayloadStore


@pytest.mark.asyncio
async def test_raw_migration_dry_run_then_verifies_and_updates_each_valid_row(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source = RawPayloadFileStore(tmp_path / "raw")
    client = FakeMinioClient()
    destination = MinioRawPayloadStore(
        client=client,
        bucket="archive",
        prefix="raw",
        create_bucket=True,
    )
    captured_at = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    valid_body = b'{"valid":true}'
    corrupt_body = b'{"corrupt":true}'
    valid_stored = source.save(
        body=valid_body,
        captured_at=captured_at,
        run_id="source-run",
        suffix=".json",
    )
    corrupt_stored = source.save(
        body=corrupt_body,
        captured_at=captured_at + timedelta(seconds=1),
        run_id="source-run",
        suffix=".jpg",
    )
    async with factory() as session:
        valid = _raw(
            captured_at=captured_at,
            stored=valid_stored,
            payload_hash=hashlib.sha256(valid_body).digest(),
        )
        corrupt = _raw(
            captured_at=captured_at + timedelta(seconds=1),
            stored=corrupt_stored,
            payload_hash=b"x" * 32,
        )
        session.add_all([valid, corrupt])
        await session.commit()
        valid_id = valid.id
        corrupt_id = corrupt.id

    service = RawPayloadMigrationService(
        session_factory=factory,
        source=source,
        destination=destination,
    )
    dry_run = await service.migrate(execute=False, limit=10)
    assert dry_run.candidate_count == 2
    assert dry_run.migrated_count == 0
    assert dry_run.failed_count == 0
    assert client.objects == {}

    executed = await service.migrate(execute=True, limit=10)
    assert executed.candidate_count == 2
    assert executed.migrated_count == 1
    assert executed.failed_count == 1
    assert executed.results[0].raw_payload_id == valid_id
    assert executed.results[0].status == "migrated"
    assert executed.results[1].raw_payload_id == corrupt_id
    assert executed.results[1].status == "failed"
    assert "hash" in (executed.results[1].error or "").casefold()

    async with factory() as session:
        valid = await session.get(RawPayload, valid_id)
        corrupt = await session.get(RawPayload, corrupt_id)
    assert valid is not None and valid.storage_uri.startswith("s3://archive/raw/")
    assert destination.read_uri(valid.storage_uri) == valid_body
    assert corrupt is not None and corrupt.storage_uri == corrupt_stored.storage_uri
    assert source.read_uri(valid_stored.storage_uri) == valid_body
    assert len(client.objects) == 1
    assert next(iter(client.objects))[1].endswith(".json.zst")
    await engine.dispose()


@pytest.mark.asyncio
async def test_raw_migration_keeps_database_uri_when_destination_verification_fails(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source = RawPayloadFileStore(tmp_path / "raw")
    body = b'{"verify":"before-update"}'
    captured_at = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    stored = source.save(
        body=body,
        captured_at=captured_at,
        run_id="source-run",
        suffix=".json",
    )
    async with factory() as session:
        raw = _raw(
            captured_at=captured_at,
            stored=stored,
            payload_hash=hashlib.sha256(body).digest(),
        )
        session.add(raw)
        await session.commit()
        raw_id = raw.id

    client = FakeMinioClient(corrupt_reads=True)
    service = RawPayloadMigrationService(
        session_factory=factory,
        source=source,
        destination=MinioRawPayloadStore(
            client=client,
            bucket="archive",
            create_bucket=True,
        ),
    )
    summary = await service.migrate(execute=True)

    async with factory() as session:
        raw = await session.get(RawPayload, raw_id)
    assert summary.failed_count == 1
    assert raw is not None and raw.storage_uri == stored.storage_uri
    assert len(client.objects) == 1
    await engine.dispose()


def _raw(*, captured_at, stored, payload_hash: bytes) -> RawPayload:
    return RawPayload(
        captured_at=captured_at,
        request_type=BilibiliRequestType.COMMENT_HOT,
        method="GET",
        url_hash=b"u" * 32,
        params_hash=None,
        status_code=200,
        payload_hash=payload_hash,
        storage_uri=stored.storage_uri,
        compressed_size=stored.compressed_size,
        uncompressed_size=stored.uncompressed_size,
        parser_version="test",
        created_at=captured_at,
    )


class FakeResponse(io.BytesIO):
    def release_conn(self) -> None:
        pass


class FakeMinioClient:
    def __init__(self, *, corrupt_reads: bool = False) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.corrupt_reads = corrupt_reads

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(
        self,
        bucket: str,
        object_name: str,
        data,
        length: int,
        **kwargs,
    ) -> None:
        body = data.read()
        assert len(body) == length
        self.objects[(bucket, object_name)] = body

    def get_object(self, bucket: str, object_name: str) -> FakeResponse:
        body = self.objects[(bucket, object_name)]
        if self.corrupt_reads:
            decoded = zstandard.ZstdDecompressor().decompress(body)
            body = zstandard.ZstdCompressor().compress(decoded + b"corrupt")
        return FakeResponse(body)
