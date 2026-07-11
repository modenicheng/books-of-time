from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import RawPayload
from books_of_time.storage.base import RawPayloadStore


@dataclass(frozen=True, slots=True)
class RawMigrationResult:
    raw_payload_id: int
    status: str
    source_uri: str
    target_uri: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RawMigrationSummary:
    execute: bool
    candidate_count: int
    migrated_count: int
    skipped_count: int
    failed_count: int
    results: tuple[RawMigrationResult, ...]


class RawPayloadMigrationService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        source: RawPayloadStore,
        destination: RawPayloadStore,
    ) -> None:
        self.session_factory = session_factory
        self.source = source
        self.destination = destination

    async def migrate(
        self,
        *,
        execute: bool,
        limit: int = 100,
        after_id: int = 0,
    ) -> RawMigrationSummary:
        if not 1 <= limit <= 10_000:
            raise ValueError("Raw migration limit must be between 1 and 10000")
        if after_id < 0:
            raise ValueError("Raw migration after_id cannot be negative")
        async with self.session_factory() as session:
            candidate_ids = list(
                await session.scalars(
                    select(RawPayload.id)
                    .where(
                        RawPayload.id > after_id,
                        RawPayload.storage_uri.startswith("file://"),
                    )
                    .order_by(RawPayload.id.asc())
                    .limit(limit)
                )
            )
        if not execute:
            async with self.session_factory() as session:
                rows = list(
                    await session.scalars(
                        select(RawPayload)
                        .where(RawPayload.id.in_(candidate_ids))
                        .order_by(RawPayload.id.asc())
                    )
                )
            results = tuple(
                RawMigrationResult(
                    raw_payload_id=row.id,
                    status="planned",
                    source_uri=row.storage_uri,
                )
                for row in rows
            )
            return _summary(execute=False, results=results)

        results: list[RawMigrationResult] = []
        for raw_payload_id in candidate_ids:
            try:
                result = await self._migrate_one(raw_payload_id)
            except Exception as exc:
                result = RawMigrationResult(
                    raw_payload_id=raw_payload_id,
                    status="failed",
                    source_uri=await self._source_uri(raw_payload_id),
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                )
            results.append(result)
        return _summary(execute=True, results=tuple(results))

    async def _migrate_one(self, raw_payload_id: int) -> RawMigrationResult:
        async with self.session_factory.begin() as session:
            row = await session.scalar(
                select(RawPayload)
                .where(RawPayload.id == raw_payload_id)
                .with_for_update()
            )
            if row is None:
                return RawMigrationResult(
                    raw_payload_id=raw_payload_id,
                    status="skipped",
                    source_uri="",
                    error="Raw payload no longer exists",
                )
            source_uri = row.storage_uri
            if not source_uri.startswith("file://"):
                return RawMigrationResult(
                    raw_payload_id=row.id,
                    status="skipped",
                    source_uri=source_uri,
                    target_uri=source_uri,
                )

            body = await asyncio.to_thread(self.source.read_uri, source_uri)
            _verify_body(row, body, location="source")
            suffix = _payload_suffix(row)
            stored = await asyncio.to_thread(
                self.destination.save,
                body=body,
                captured_at=row.captured_at,
                run_id=f"migration-{row.id}",
                suffix=suffix,
            )
            if stored.payload_hash_hex != row.payload_hash.hex():
                raise ValueError("Destination save returned a different payload hash")
            verified = await asyncio.to_thread(
                self.destination.read_uri,
                stored.storage_uri,
            )
            _verify_body(row, verified, location="destination")

            row.storage_uri = stored.storage_uri
            row.compressed_size = stored.compressed_size
            row.uncompressed_size = stored.uncompressed_size
            await session.flush()
            return RawMigrationResult(
                raw_payload_id=row.id,
                status="migrated",
                source_uri=source_uri,
                target_uri=stored.storage_uri,
            )

    async def _source_uri(self, raw_payload_id: int) -> str:
        async with self.session_factory() as session:
            row = await session.get(RawPayload, raw_payload_id)
            return row.storage_uri if row is not None else ""


def _verify_body(row: RawPayload, body: bytes, *, location: str) -> None:
    digest = hashlib.sha256(body).digest()
    if digest != row.payload_hash:
        raise ValueError(f"Raw payload {location} hash does not match database")
    if len(body) != row.uncompressed_size:
        raise ValueError(f"Raw payload {location} size does not match database")


def _payload_suffix(row: RawPayload) -> str:
    filename = row.storage_uri.replace("\\", "/").rsplit("/", 1)[-1]
    digest = row.payload_hash.hex()
    if not filename.startswith(digest) or not filename.endswith(".zst"):
        raise ValueError("Raw payload filename does not preserve its content hash")
    suffix = filename[len(digest) : -len(".zst")]
    if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise ValueError("Raw payload filename has an invalid suffix")
    return suffix


def _summary(
    *,
    execute: bool,
    results: tuple[RawMigrationResult, ...],
) -> RawMigrationSummary:
    return RawMigrationSummary(
        execute=execute,
        candidate_count=len(results),
        migrated_count=sum(row.status == "migrated" for row in results),
        skipped_count=sum(row.status == "skipped" for row in results),
        failed_count=sum(row.status == "failed" for row in results),
        results=results,
    )
