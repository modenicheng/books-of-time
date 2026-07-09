from books_of_time.cli import build_parser


def test_collect_latest_comments_parser_defaults() -> None:
    args = build_parser().parse_args(["collect-latest-comments", "BV1abc"])

    assert args.command == "collect-latest-comments"
    assert args.bvid == "BV1abc"
    assert args.priority == 70
    assert args.max_scan_seconds == 55


def test_collect_latest_comments_parser_accepts_overrides() -> None:
    args = build_parser().parse_args(
        [
            "collect-latest-comments",
            "BV1abc",
            "--priority",
            "90",
            "--max-scan-seconds",
            "12",
        ]
    )

    assert args.priority == 90
    assert args.max_scan_seconds == 12
