from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.app import build_worker
from books_of_time.domain.enums import TaskKind
from books_of_time.domain.watchlist import (
    WatchlistPolicy,
    calculate_watchlist_priority,
)


def test_watchlist_priority_combines_signals_and_preserves_evidence() -> None:
    result = calculate_watchlist_priority(
        policy=WatchlistPolicy(controversy_keywords=("控评", "删评")),
        content="质疑控评和删评",
        sort_mode="hot",
        position=2,
        previous_reply_count=1,
        current_reply_count=9,
        previous_like_count=10,
        current_like_count=50,
        is_first_seen=True,
    )

    assert result is not None
    assert result.reason == "hot_top"
    assert result.priority == 100
    assert result.extra == {
        "controversy_keywords": ["控评", "删评"],
        "hot_position": 2,
        "like_delta": 40,
        "recent_first_seen": True,
        "reply_delta": 8,
    }


def test_watchlist_priority_supports_like_growth_without_reply_growth() -> None:
    result = calculate_watchlist_priority(
        policy=WatchlistPolicy(like_growth_min=20),
        content="ordinary",
        sort_mode="latest",
        position=20,
        previous_reply_count=1,
        current_reply_count=1,
        previous_like_count=10,
        current_like_count=40,
        is_first_seen=False,
    )

    assert result is not None
    assert result.reason == "like_growth"
    assert result.extra == {"like_delta": 30}


def test_recent_first_seen_is_a_bonus_not_a_standalone_trigger() -> None:
    result = calculate_watchlist_priority(
        policy=WatchlistPolicy(),
        content="ordinary",
        sort_mode="latest",
        position=20,
        previous_reply_count=None,
        current_reply_count=0,
        previous_like_count=None,
        current_like_count=0,
        is_first_seen=True,
    )

    assert result is None


def test_watchlist_policy_normalizes_configured_keywords() -> None:
    policy = WatchlistPolicy.from_config(
        {
            "like_growth_min": 30,
            "controversy_keywords": [" 控评 ", "控评", "DELETE"],
            "recent_first_seen_bonus": 4,
        }
    )

    assert policy.like_growth_min == 30
    assert policy.controversy_keywords == ("控评", "delete")
    assert policy.recent_first_seen_bonus == 4


def test_build_worker_injects_same_watchlist_policy_into_comment_collectors(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = _MinimalClient()
    worker = build_worker(
        {
            "database": {"url": "sqlite+aiosqlite:///:memory:"},
            "storage": {
                "raw_dir": str(tmp_path / "raw"),
                "media_dir": str(tmp_path / "media"),
            },
            "watchlist": {
                "like_growth_min": 30,
                "controversy_keywords": ["控评"],
            },
        },
        run_id="watchlist-policy-test",
        lease_owner="test-worker",
        session_factory=session_factory,
        client=client,
    )

    policies = [
        worker.collectors[kind].watchlist_policy
        for kind in (
            TaskKind.FETCH_HOT_COMMENTS,
            TaskKind.FETCH_LATEST_COMMENTS,
            TaskKind.FETCH_COMMENT_REPLIES,
        )
    ]
    assert all(policy is policies[0] for policy in policies)
    assert policies[0].like_growth_min == 30
    assert policies[0].controversy_keywords == ("控评",)


class _MinimalClient:
    http_client = object()
    rate_limiter = None
