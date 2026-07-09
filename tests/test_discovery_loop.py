from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import CollectionTask, KnownVideo
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.task_orchestrator.discovery_loop import DiscoveryLoop


class FakeDiscoveryClient:
    def __init__(self, responses: dict[str, FetchResult | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult:
        self.calls.append((mid, page))
        response = self.responses[mid]
        if isinstance(response, Exception):
            raise response
        return response


def _video_list_result(mid: str, *bvids: str, captured_at: datetime) -> FetchResult:
    payload = {
        "data": {
            "list": {
                "vlist": [
                    {"bvid": bvid, "created": int(captured_at.timestamp())}
                    for bvid in bvids
                ]
            }
        }
    }
    return FetchResult(
        request_type=BilibiliRequestType.USER_VIDEO_LIST,
        method="GET",
        url=f"https://api.bilibili.com/x/space/wbi/arc/search?mid={mid}",
        params={"mid": mid, "pn": 1},
        status_code=200,
        body=json.dumps(payload).encode(),
        captured_at=captured_at,
        response_headers={},
    )


@pytest.mark.asyncio
async def test_discovery_loop_scans_configured_uids_and_enqueues_tasks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2099, 1, 1, tzinfo=UTC)
    client = FakeDiscoveryClient(
        {
            "100": _video_list_result("100", "BV100", captured_at=now),
            "200": _video_list_result("200", "BV200", captured_at=now),
        }
    )
    loop = DiscoveryLoop(
        session_factory=session_factory,
        client=client,
        matrix_uids=["100", "200"],
    )

    result = await loop.run_once(now=now)

    async with session_factory() as session:
        known = list(await session.scalars(select(KnownVideo)))
        tasks = list(await session.scalars(select(CollectionTask)))

    assert result.uids_scanned == 2
    assert result.videos_seen == 2
    assert result.videos_created == 2
    assert result.errors == 0
    assert client.calls == [("100", 1), ("200", 1)]
    assert [video.bvid for video in known] == ["BV100", "BV200"]
    assert [task.target_id for task in tasks] == ["BV100", "BV200"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_loop_continues_after_uid_failure() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2099, 1, 1, tzinfo=UTC)
    client = FakeDiscoveryClient(
        {
            "bad": RuntimeError("request failed"),
            "good": _video_list_result("good", "BVGOOD", captured_at=now),
        }
    )
    loop = DiscoveryLoop(
        session_factory=session_factory,
        client=client,
        matrix_uids=["bad", "good"],
    )

    result = await loop.run_once(now=now)

    async with session_factory() as session:
        tasks = list(await session.scalars(select(CollectionTask)))

    assert result.uids_scanned == 1
    assert result.videos_seen == 1
    assert result.videos_created == 1
    assert result.errors == 1
    assert [task.target_id for task in tasks] == ["BVGOOD"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_loop_run_loop_uses_injected_sleep() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    loop = DiscoveryLoop(
        session_factory=session_factory,
        client=FakeDiscoveryClient({}),
        matrix_uids=[],
    )

    result = await loop.run_loop(
        interval_seconds=0.5,
        max_iterations=2,
        sleep=fake_sleep,
    )

    assert result.uids_scanned == 0
    assert slept == [0.5]

    await engine.dispose()
