import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base
from books_of_time.db.repositories import EventRepository


def test_event_report_cli_parser_exposes_evidence_report_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "event",
            "report",
            "event-a",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--bucket-minutes",
            "30",
            "--top-n",
            "10",
            "--bvid",
            "BV1xx411c7mD",
            "--keyword",
            "控评",
            "--output",
            "report.md",
            "--json-output",
            "report.json",
        ]
    )

    assert args.event_command == "report"
    assert args.bucket_minutes == 30
    assert args.top_n == 10
    assert args.bvid == "BV1xx411c7mD"
    assert args.keyword == "控评"
    assert args.json_output == "report.json"


@pytest.mark.asyncio
async def test_event_report_cli_writes_markdown_and_structured_json(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'report.sqlite3'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await EventRepository(session).create_event(
            slug="event-a",
            name="事件 A",
            now=datetime(2026, 7, 10, tzinfo=UTC),
        )
        await session.commit()
    await engine.dispose()

    markdown_path = tmp_path / "reports" / "event-a.md"
    json_path = tmp_path / "reports" / "event-a.json"
    report = await cli._export_event_report(
        {"database": {"url": database_url}},
        event_reference="event-a",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        bucket_minutes=60,
        spike_multiplier=3.0,
        spike_min_count=5,
        turnover_threshold=0.5,
        top_n=20,
        template_window_minutes=60,
        template_min_similarity=0.85,
        template_min_text_chars=8,
        max_videos=100,
        max_records=200_000,
        bvid=None,
        keyword=None,
        output_path=markdown_path,
        json_output_path=json_path,
    )

    assert report.event["slug"] == "event-a"
    assert "## 结论限制" in markdown_path.read_text(encoding="utf-8")
    structured = json.loads(json_path.read_text(encoding="utf-8"))
    assert structured["schema_version"] == "event-report-v1"
    assert structured["event"]["slug"] == "event-a"
    assert structured["filters"] == {"bvid": None, "keyword": None}
