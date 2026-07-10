from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from books_of_time import cli
from books_of_time.db.base import Base


def test_video_metric_replay_parser_accepts_window_and_limit() -> None:
    args = cli.build_parser().parse_args(
        [
            "video",
            "replay-metrics",
            "BV1xx411c7mD",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--max-points",
            "5000",
            "--output",
            "metrics.jsonl",
        ]
    )
    assert args.video_command == "replay-metrics"
    assert args.max_points == 5000

    hot = cli.build_parser().parse_args(
        [
            "video",
            "replay-hot-comments",
            "BV1xx411c7mD",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--top-n",
            "10",
            "--output",
            "hot.jsonl",
        ]
    )
    assert hot.video_command == "replay-hot-comments"
    assert hot.top_n == 10

    visibility = cli.build_parser().parse_args(
        [
            "video",
            "replay-visibility",
            "BV1xx411c7mD",
            "--since",
            "2026-07-10T00:00:00Z",
            "--until",
            "2026-07-10T02:00:00Z",
            "--output",
            "visibility.jsonl",
        ]
    )
    assert visibility.video_command == "replay-visibility"


@pytest.mark.asyncio
async def test_video_metric_replay_export_writes_empty_jsonl(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'metrics.sqlite3'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    output = tmp_path / "out" / "metrics.jsonl"
    count = await cli._export_video_metric_replay(
        {"database": {"url": url}},
        bvid="BV1xx411c7mD",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        max_points=5000,
        output_path=output,
    )
    assert count == 0
    assert output.read_text(encoding="utf-8") == ""

    hot_output = tmp_path / "out" / "hot.jsonl"
    hot_count = await cli._export_hot_comment_replay(
        {"database": {"url": url}},
        bvid="BV1xx411c7mD",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        top_n=10,
        max_snapshots=5000,
        output_path=hot_output,
    )
    assert hot_count == 0
    assert hot_output.read_text(encoding="utf-8") == ""

    visibility_output = tmp_path / "out" / "visibility.jsonl"
    visibility_count = await cli._export_comment_visibility_replay(
        {"database": {"url": url}},
        bvid="BV1xx411c7mD",
        since="2026-07-10T00:00:00Z",
        until="2026-07-10T02:00:00Z",
        max_events=5000,
        output_path=visibility_output,
    )
    assert visibility_count == 0
    assert visibility_output.read_text(encoding="utf-8") == ""
