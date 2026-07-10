import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.migrations import (
    get_current_schema_revision,
    get_expected_schema_revision,
)
from books_of_time.service.health import ServiceHealthChecker


@pytest.mark.asyncio
async def test_schema_revision_helpers_read_expected_and_current_head(
    tmp_path: Path,
) -> None:
    expected = get_expected_schema_revision()
    assert expected == "0004_comment_analysis_flags"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": expected},
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        assert await get_current_schema_revision(session) == expected
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_doctor_rejects_missing_schema_revision(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    checker = ServiceHealthChecker(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        raw_dir=tmp_path / "raw",
        media_dir=tmp_path / "media",
        expected_schema_revision="0001_initial",
    )

    report = await checker.doctor()
    revision = next(check for check in report.checks if check.name == "schema_revision")

    assert report.ok is False
    assert revision.ok is False
    assert "missing" in revision.detail
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_revision", "expected_ok"),
    [("0001_initial", True), ("old_revision", False)],
)
async def test_service_doctor_compares_schema_revision(
    tmp_path: Path,
    stored_revision: str,
    expected_ok: bool,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": stored_revision},
        )
    checker = ServiceHealthChecker(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        raw_dir=tmp_path / "raw",
        media_dir=tmp_path / "media",
        expected_schema_revision="0001_initial",
    )

    report = await checker.doctor()
    revision = next(check for check in report.checks if check.name == "schema_revision")

    assert revision.ok is expected_ok
    assert report.ok is expected_ok
    await engine.dispose()


def test_initial_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_initial.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert "Base.metadata" not in source
    assert "def upgrade()" in source
    assert "def downgrade()" in source


def test_event_archive_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0002_event_archive.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0001_initial"' in source
    assert "Base.metadata" not in source
    assert 'op.create_table(\n        "events"' in source
    assert 'op.create_table(\n        "event_targets"' in source
    assert 'op.create_table(\n        "event_videos"' in source
    assert 'op.create_table(\n        "event_keywords"' in source


def test_account_cookie_refresh_revision_extends_postgresql_enum_safely() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0003_account_cookie_refresh_job.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0002_event_archive"' in source
    assert "ADD VALUE IF NOT EXISTS 'account_cookie_refresh'" in source
    assert "DELETE FROM scheduled_jobs" in source
    assert "Base.metadata" not in source


def test_comment_analysis_flags_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0004_comment_analysis_flags.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        "down_revision: str | Sequence[str] | None = "
        '"0003_account_cookie_refresh_job"' in source
    )
    assert 'op.create_table(\n        "comment_analysis_flags"' in source
    assert 'op.create_index(\n        "idx_comment_analysis_flags_event_type"' in source
    assert "Base.metadata" not in source


def test_importing_migration_helpers_does_not_load_autogenerate_plugins() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import books_of_time.db.migrations; "
            "print(any(name.startswith('alembic.autogenerate') "
            "for name in sys.modules))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"
