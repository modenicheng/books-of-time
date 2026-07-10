from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import Event, EventTarget
from books_of_time.domain.events import (
    normalize_event_slug,
    normalize_event_target,
    validate_event_window,
)


def test_event_normalization_preserves_verifiable_stable_values() -> None:
    assert normalize_event_slug("  Ghost-Picture-War ") == "ghost-picture-war"
    assert normalize_event_target("uid", " 00123 ") == "123"
    assert normalize_event_target("keyword", "  鬼 图   战争 ") == "鬼 图 战争"
    assert normalize_event_target("game", "  Honkai  Star Rail ") == (
        "honkai star rail"
    )
    assert normalize_event_target("seed_bvid", " BV1xx411c7mD ") == ("BV1xx411c7mD")


@pytest.mark.parametrize(
    ("target_type", "value"),
    [
        ("uid", "not-a-number"),
        ("uid", "0"),
        ("keyword", "   "),
        ("game", ""),
        ("seed_bvid", "BV-short"),
        ("unknown", "value"),
    ],
)
def test_event_target_normalization_rejects_invalid_values(
    target_type: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_event_target(target_type, value)


def test_event_window_rejects_end_before_start() -> None:
    start = datetime(2026, 7, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="end_at"):
        validate_event_window(start, start - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_event_target_stable_key_is_unique() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        event = Event(
            slug="ghost-picture-war",
            name="鬼图战争",
            game="example-game",
            description=None,
            status="active",
            start_at=now,
            end_at=None,
            timezone="Asia/Shanghai",
            created_at=now,
            updated_at=now,
        )
        session.add(event)
        await session.flush()
        for original in ("123", "00123"):
            session.add(
                EventTarget(
                    event_id=event.id,
                    target_type="uid",
                    target_value=original,
                    normalized_value="123",
                    priority=100,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                    extra={},
                    created_at=now,
                    updated_at=now,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()
