from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base


def test_hot_turnover_parser_accepts_window_and_top_n() -> None:
    args = cli.build_parser().parse_args(
        [
            "video",
            "hot-turnover",
            "BV1xx411c7mD",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--top-n",
            "10",
            "--output",
            "turnover.jsonl",
        ]
    )
    assert args.video_command == "hot-turnover"
    assert args.top_n == 10


@pytest.mark.asyncio
async def test_hot_turnover_export_writes_empty_jsonl(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'turnover.sqlite3'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    output = tmp_path / "out" / "turnover.jsonl"
    count = await cli._export_hot_turnover(
        {"database": {"url": database_url}},
        bvid="BV1xx411c7mD",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        top_n=10,
        output_path=output,
    )

    assert count == 0
    assert output.read_text(encoding="utf-8") == ""
