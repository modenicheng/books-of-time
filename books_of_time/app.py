from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from books_of_time.accounts.manager import AccountManager
from books_of_time.accounts.provider import CurrentCookieProvider
from books_of_time.accounts.storage import EncryptedFileCredentialStore
from books_of_time.collectors.hot_comments import HotCommentCollector
from books_of_time.collectors.latest_comments import LatestCommentCollector
from books_of_time.collectors.reply_comments import ReplyCommentCollector
from books_of_time.collectors.user_videos import UserVideosCollector
from books_of_time.collectors.video_stats import VideoStatsCollector
from books_of_time.domain.enums import TaskKind
from books_of_time.domain.watchlist import WatchlistPolicy
from books_of_time.http.client import RawHttpClient
from books_of_time.http.rate_limiter import RateLimitRule, TokenBucketRateLimiter
from books_of_time.media.downloader import MediaAssetCollector, MediaDownloader
from books_of_time.media.similarity import MediaSimilarityCollector
from books_of_time.media.storage import MediaStore
from books_of_time.platforms.bilibili.client import BilibiliPlatformClient
from books_of_time.service.coordinator import ScheduledJobCoordinator
from books_of_time.service.scheduled_jobs import build_default_scheduled_jobs
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.task_orchestrator.video_snapshot_scheduler import (
    VideoSnapshotScheduler,
)
from books_of_time.worker import Worker


def build_engine(cfg: dict[str, Any]) -> AsyncEngine:
    db_cfg = cfg["database"]
    return create_async_engine(
        db_cfg["url"],
        pool_size=db_cfg.get("pool_size", 5),
        max_overflow=db_cfg.get("max_overflow", 10),
        pool_pre_ping=db_cfg.get("pool_pre_ping", True),
        echo=db_cfg.get("echo", False),
    )


def build_session_factory(
    cfg: dict[str, Any],
    *,
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    effective_engine = engine or build_engine(cfg)
    return async_sessionmaker(effective_engine, expire_on_commit=False)


def build_rate_limiter(cfg: dict[str, Any]) -> TokenBucketRateLimiter:
    rules = {
        key: RateLimitRule(rps=float(value["rps"]), burst=int(value["burst"]))
        for key, value in cfg.get("rate_limit", {}).items()
    }
    return TokenBucketRateLimiter(rules)


def build_bilibili_client(cfg: dict[str, Any]) -> BilibiliPlatformClient:
    http_cfg = cfg.get("http", {})
    cookie_provider = build_cookie_provider(cfg)
    return BilibiliPlatformClient(
        http_client=RawHttpClient(
            timeout_seconds=float(http_cfg.get("timeout_seconds", 10)),
            user_agent=str(
                http_cfg.get("user_agent", "BooksOfTime/0.1 research collector")
            ),
            cookie_provider=cookie_provider,
        ),
        rate_limiter=build_rate_limiter(cfg),
    )


def build_credential_store(cfg: dict[str, Any]) -> EncryptedFileCredentialStore:
    account_cfg = cfg.get("accounts", {})
    return EncryptedFileCredentialStore(
        credentials_path=Path(
            account_cfg.get(
                "credentials_path",
                "./data/accounts/credentials.enc",
            )
        ),
        key_path=Path(account_cfg.get("key_path", "./data/accounts/master.key")),
        history_limit=int(account_cfg.get("history_limit", 5)),
    )


def build_cookie_provider(
    cfg: dict[str, Any],
) -> CurrentCookieProvider | None:
    account_cfg = cfg.get("accounts", {})
    if not bool(account_cfg.get("enabled", True)):
        return None
    return CurrentCookieProvider(
        store=build_credential_store(cfg),
        default_account_id=str(account_cfg.get("active_account_id", "default")),
    )


def build_account_manager(cfg: dict[str, Any]) -> AccountManager:
    account_cfg = cfg.get("accounts", {})
    return AccountManager(
        store=build_credential_store(cfg),
        default_account_id=str(account_cfg.get("active_account_id", "default")),
    )


def build_worker(
    cfg: dict[str, Any],
    *,
    run_id: str,
    lease_owner: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: BilibiliPlatformClient | None = None,
) -> Worker:
    effective_session_factory = session_factory or build_session_factory(cfg)
    effective_client = client or build_bilibili_client(cfg)
    storage_cfg = cfg.get("storage", {})
    raw_dir = Path(storage_cfg.get("raw_dir", "./data/raw"))
    media_dir = Path(storage_cfg.get("media_dir", "./data/media"))
    raw_store = RawPayloadFileStore(raw_dir)
    scheduler_cfg = cfg.get("scheduler", {})
    latest_comments_cfg = cfg.get("latest_comments", {})
    watchlist_policy = WatchlistPolicy.from_config(cfg.get("watchlist"))
    return Worker(
        session_factory=effective_session_factory,
        collectors={
            TaskKind.FETCH_VIDEO_STATS: VideoStatsCollector(
                client=effective_client,
                raw_store=raw_store,
                run_id=run_id,
                snapshot_scheduler=VideoSnapshotScheduler(),
            ),
            TaskKind.FETCH_HOT_COMMENTS: HotCommentCollector(
                client=effective_client,
                raw_store=raw_store,
                run_id=run_id,
                watchlist_policy=watchlist_policy,
            ),
            TaskKind.FETCH_LATEST_COMMENTS: LatestCommentCollector(
                client=effective_client,
                raw_store=raw_store,
                run_id=run_id,
                max_scan_seconds=float(latest_comments_cfg.get("max_scan_seconds", 55)),
                page_retry_attempts=int(
                    latest_comments_cfg.get("page_retry_attempts", 3)
                ),
                page_retry_backoff_seconds=[
                    float(value)
                    for value in latest_comments_cfg.get(
                        "page_retry_backoff_seconds",
                        [1, 3, 5],
                    )
                ],
                watchlist_policy=watchlist_policy,
            ),
            TaskKind.FETCH_COMMENT_REPLIES: ReplyCommentCollector(
                client=effective_client,
                raw_store=raw_store,
                run_id=run_id,
                watchlist_policy=watchlist_policy,
            ),
            TaskKind.DISCOVER_USER_VIDEOS: UserVideosCollector(
                client=effective_client,
                raw_store=raw_store,
                run_id=run_id,
                session_factory=effective_session_factory,
            ),
            TaskKind.FETCH_MEDIA_ASSET: MediaAssetCollector(
                MediaDownloader(
                    http_client=effective_client.http_client,
                    rate_limiter=effective_client.rate_limiter,
                    media_store=MediaStore(media_dir),
                    raw_store=raw_store,
                    run_id=run_id,
                )
            ),
            TaskKind.ANALYZE_SIMILAR_MEDIA: MediaSimilarityCollector(),
        },
        run_id=run_id,
        lease_owner=lease_owner,
        lease_seconds=int(scheduler_cfg.get("lease_seconds", 120)),
        retry_delay_seconds=int(scheduler_cfg.get("default_retry_delay_seconds", 300)),
    )


def build_service_coordinator(
    cfg: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    instance_id: str,
    client: BilibiliPlatformClient | None = None,
) -> ScheduledJobCoordinator:
    effective_client = client or build_bilibili_client(cfg)
    definitions, handlers = build_default_scheduled_jobs(
        cfg,
        account_manager=build_account_manager(cfg),
        bilibili_client=effective_client,
    )
    scheduler_cfg = cfg.get("scheduler", {})
    service_cfg = cfg.get("service", {})
    return ScheduledJobCoordinator(
        session_factory=session_factory,
        definitions=definitions,
        handlers=handlers,
        lease_owner=instance_id,
        lease_seconds=int(service_cfg.get("scheduler_lease_seconds", 60)),
        retry_delay_seconds=int(scheduler_cfg.get("default_retry_delay_seconds", 300)),
        idle_sleep_seconds=float(service_cfg.get("scheduler_idle_sleep_seconds", 1)),
    )
