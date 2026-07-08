# Phase 1A Hot Comments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first audited Bilibili hot-comment collection slice: enqueue a BV task, fetch hot comments page 1, archive raw evidence, record page meaning, upsert public comment entities, and append comment observations.

**Architecture:** Follow the existing Books of Time pattern: platform clients only construct and capture Bilibili requests, parsers normalize raw JSON into dataclasses, repositories write ORM rows, collectors orchestrate one task inside the worker transaction, and CLI commands enqueue tasks. Phase 1A keeps the flow BV-based by extracting `aid` from the existing video info response inside the collector.

**Tech Stack:** Python 3.12, uv, pytest + pytest-asyncio, Ruff, SQLAlchemy asyncio ORM, SQLite in tests, PostgreSQL-compatible ORM models, bilibili-api-python, curl-cffi raw HTTP backend, zstandard raw payload storage.

## Global Constraints

- Comment authors are **not anonymized** in this project.
- Store public author fields as evidence attached to public comments.
- Do not infer private identity.
- Do not label ordinary users.
- Do not build cross-event behavioral judgments.
- Do not hide collection limits or failed request windows.
- The hash does **not** replace the readable text.
- Phase 1A will use `first_content_hash` for the first observed text fingerprint on `comment_entities`, and `content_hash` for each observation's current text.
- Phase 1A is scoped to hot comment page 1; latest comments and coverage tables are explicitly later slices.
- JSON extension fields use `extra`, avoiding SQLAlchemy's reserved `metadata` attribute name.
- Final verification commands are `uv run pytest` and `uv run ruff check .`.

---

## File Structure

- Create `books_of_time/parsers/comments.py`: dataclasses, content hashing, parser exception, and `parse_hot_comment_page`.
- Modify `books_of_time/db/models.py`: add `RawPageObservation`, `CommentEntity`, and `CommentObservation` ORM models and indexes.
- Modify `books_of_time/db/repositories.py`: add `RawPageObservationRepository` and `CommentRepository`.
- Modify `books_of_time/platforms/bilibili/client.py`: add `get_hot_comments`.
- Modify `tests/test_bilibili_client.py`: add a fake comment API and client routing test.
- Create `tests/test_comments_parser.py`: parser happy-path and malformed-payload tests.
- Create `tests/test_comment_repositories.py`: entity upsert and observation append test.
- Create `books_of_time/collectors/hot_comments.py`: collector that fetches video info, extracts `aid`, fetches hot comments, saves raw, writes page and comment rows.
- Modify `books_of_time/app.py`: register `TaskKind.FETCH_HOT_COMMENTS` collector.
- Create `tests/test_hot_comments_worker.py`: worker integration test around fake Bilibili client and raw store.
- Modify `books_of_time/cli.py`: add `bot video comments BV --mode hot --priority N`.
- Modify `docs/TODO.md`: mark completed Phase 1A items after verified implementation.

---

### Task 1: Comment Parser

**Files:**
- Create: `books_of_time/parsers/comments.py`
- Test: `tests/test_comments_parser.py`

**Interfaces:**
- Consumes: raw Bilibili comment page payload shaped as `{"code": 0, "data": {"replies": [...]}}`.
- Produces:
  - `COMMENT_PARSER_VERSION: str`
  - `CommentParseError(Exception)`
  - `ParsedComment`
  - `ParsedCommentPage`
  - `hash_comment_content(content: str | None) -> bytes`
  - `parse_hot_comment_page(payload: dict[str, Any], *, bvid: str, oid: int, captured_at: datetime, raw_payload_id: int, page_number: int) -> ParsedCommentPage`

- [ ] **Step 1: Write parser tests**

Create `tests/test_comments_parser.py`:

```python
from datetime import UTC, datetime

import pytest

from books_of_time.parsers.comments import (
    CommentParseError,
    hash_comment_content,
    parse_hot_comment_page,
)


def test_parse_hot_comment_page_extracts_public_comment_fields() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    payload = {
        "code": 0,
        "data": {
            "cursor": {"all_count": 2},
            "replies": [
                {
                    "rpid": 1001,
                    "oid": 777,
                    "root": 0,
                    "parent": 0,
                    "like": 12,
                    "rcount": 3,
                    "member": {"mid": "42", "uname": "Alice"},
                    "content": {"message": "first comment"},
                },
                {
                    "rpid": 1002,
                    "oid": 777,
                    "root": 1001,
                    "parent": 1001,
                    "like": 5,
                    "rcount": 0,
                    "member": {"mid": 84, "uname": "Bob"},
                    "content": {"message": "reply comment"},
                },
            ],
        },
    }

    page = parse_hot_comment_page(
        payload,
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=42,
        page_number=1,
    )

    assert page.bvid == "BV1abc"
    assert page.oid == 777
    assert page.captured_at == captured_at
    assert page.raw_payload_id == 42
    assert page.sort_mode == "hot"
    assert page.page_number == 1
    assert page.extra == {"all_count": 2}
    assert len(page.comments) == 2

    first = page.comments[0]
    assert first.rpid == 1001
    assert first.root_rpid is None
    assert first.parent_rpid is None
    assert first.author_mid == 42
    assert first.author_name == "Alice"
    assert first.content == "first comment"
    assert first.content_hash == hash_comment_content("first comment")
    assert first.like_count == 12
    assert first.reply_count == 3
    assert first.position == 1

    second = page.comments[1]
    assert second.root_rpid == 1001
    assert second.parent_rpid == 1001
    assert second.author_mid == 84
    assert second.author_name == "Bob"
    assert second.position == 2


def test_parse_hot_comment_page_rejects_missing_replies_list() -> None:
    with pytest.raises(CommentParseError, match="data.replies"):
        parse_hot_comment_page(
            {"code": 0, "data": {"replies": None}},
            bvid="BV1abc",
            oid=777,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            raw_payload_id=42,
            page_number=1,
        )


def test_parse_hot_comment_page_rejects_nonzero_code() -> None:
    with pytest.raises(CommentParseError, match="code"):
        parse_hot_comment_page(
            {"code": -400, "message": "bad request", "data": {"replies": []}},
            bvid="BV1abc",
            oid=777,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            raw_payload_id=42,
            page_number=1,
        )
```

- [ ] **Step 2: Run parser tests and verify failure**

Run:

```bash
uv run pytest tests/test_comments_parser.py -v
```

Expected: fails with `ModuleNotFoundError: No module named 'books_of_time.parsers.comments'`.

- [ ] **Step 3: Implement parser**

Create `books_of_time/parsers/comments.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

COMMENT_PARSER_VERSION = "comments.v1"


class CommentParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedComment:
    rpid: int
    oid: int
    bvid: str
    root_rpid: int | None
    parent_rpid: int | None
    author_mid: int | None
    author_name: str | None
    content: str | None
    content_hash: bytes
    like_count: int | None
    reply_count: int | None
    position: int


@dataclass(frozen=True)
class ParsedCommentPage:
    bvid: str
    oid: int
    captured_at: datetime
    raw_payload_id: int
    sort_mode: str
    page_number: int
    comments: list[ParsedComment]
    extra: dict[str, Any]


def hash_comment_content(content: str | None) -> bytes:
    normalized = (content or "").strip()
    return hashlib.sha256(normalized.encode()).digest()


def parse_hot_comment_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
) -> ParsedCommentPage:
    code = payload.get("code")
    if code not in (0, None):
        raise CommentParseError(f"Bilibili comment response code is not 0: {code}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CommentParseError("Bilibili comment response data is not an object")

    replies = data.get("replies")
    if not isinstance(replies, list):
        raise CommentParseError("Bilibili comment response data.replies is not a list")

    comments = [
        _parse_comment(
            item,
            bvid=bvid,
            fallback_oid=oid,
            position=index,
        )
        for index, item in enumerate(replies, start=1)
        if isinstance(item, dict)
    ]
    return ParsedCommentPage(
        bvid=bvid,
        oid=oid,
        captured_at=captured_at,
        raw_payload_id=raw_payload_id,
        sort_mode="hot",
        page_number=page_number,
        comments=comments,
        extra=_page_extra(data),
    )


def _parse_comment(
    item: dict[str, Any],
    *,
    bvid: str,
    fallback_oid: int,
    position: int,
) -> ParsedComment:
    content = item.get("content")
    member = item.get("member")
    message = content.get("message") if isinstance(content, dict) else None
    oid = _int_or_none(item.get("oid")) or fallback_oid
    root = _zero_as_none(_int_or_none(item.get("root")))
    parent = _zero_as_none(_int_or_none(item.get("parent")))
    return ParsedComment(
        rpid=_required_int(item.get("rpid"), "rpid"),
        oid=oid,
        bvid=bvid,
        root_rpid=root,
        parent_rpid=parent,
        author_mid=_int_or_none(member.get("mid")) if isinstance(member, dict) else None,
        author_name=str(member.get("uname")) if isinstance(member, dict) and member.get("uname") is not None else None,
        content=message if isinstance(message, str) else None,
        content_hash=hash_comment_content(message if isinstance(message, str) else None),
        like_count=_int_or_none(item.get("like")),
        reply_count=_int_or_none(item.get("rcount")),
        position=position,
    )


def _page_extra(data: dict[str, Any]) -> dict[str, Any]:
    cursor = data.get("cursor")
    if not isinstance(cursor, dict):
        return {}
    extra: dict[str, Any] = {}
    for key in ("all_count", "is_begin", "is_end", "next", "prev"):
        if key in cursor:
            extra[key] = cursor[key]
    return extra


def _required_int(value: Any, field_name: str) -> int:
    parsed = _int_or_none(value)
    if parsed is None:
        raise CommentParseError(f"Bilibili comment field {field_name} is required")
    return parsed


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _zero_as_none(value: int | None) -> int | None:
    if value == 0:
        return None
    return value
```

- [ ] **Step 4: Run parser tests and verify pass**

Run:

```bash
uv run pytest tests/test_comments_parser.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit parser task**

Run:

```bash
git add books_of_time/parsers/comments.py tests/test_comments_parser.py
git commit -m "feat: parse hot comment pages"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 2: Comment ORM Models And Repositories

**Files:**
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Test: `tests/test_comment_repositories.py`

**Interfaces:**
- Consumes from Task 1:
  - `ParsedCommentPage`
  - `ParsedComment`
- Produces:
  - ORM models: `RawPageObservation`, `CommentEntity`, `CommentObservation`
  - `RawPageObservationRepository.insert_from_parsed_page(parsed: ParsedCommentPage, request_type: BilibiliRequestType) -> RawPageObservation`
  - `CommentRepository.upsert_page(parsed: ParsedCommentPage, raw_page_observation_id: int) -> list[CommentObservation]`

- [ ] **Step 1: Write repository test**

Create `tests/test_comment_repositories.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import (
    Base,
    CommentEntity,
    CommentObservation,
    RawPageObservation,
)
from books_of_time.db.repositories import (
    CommentRepository,
    RawPageObservationRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.parsers.comments import ParsedComment, ParsedCommentPage


@pytest.mark.asyncio
async def test_comment_repository_upserts_entity_and_appends_observations() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    content_hash = b"x" * 32
    parsed = ParsedCommentPage(
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=42,
        sort_mode="hot",
        page_number=1,
        comments=[
            ParsedComment(
                rpid=1001,
                oid=777,
                bvid="BV1abc",
                root_rpid=None,
                parent_rpid=None,
                author_mid=42,
                author_name="Alice",
                content="first comment",
                content_hash=content_hash,
                like_count=12,
                reply_count=3,
                position=1,
            )
        ],
        extra={"all_count": 1},
    )

    async with session_factory() as session:
        page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=page.id,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=page.id,
        )
        await session.commit()

    async with session_factory() as session:
        entity_count = await session.scalar(select(func.count(CommentEntity.rpid)))
        observation_count = await session.scalar(select(func.count(CommentObservation.id)))
        raw_page = await session.scalar(select(RawPageObservation))
        entity = await session.scalar(select(CommentEntity))
        observations = (
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.id.asc())
            )
        ).all()

        assert entity_count == 1
        assert observation_count == 2
        assert raw_page is not None
        assert raw_page.item_count == 1
        assert raw_page.extra == {"all_count": 1}
        assert entity is not None
        assert entity.rpid == 1001
        assert entity.author_mid == 42
        assert entity.author_name == "Alice"
        assert entity.first_content == "first comment"
        assert entity.first_content_hash == content_hash
        assert observations[0].raw_page_observation_id == raw_page.id
        assert observations[0].content == "first comment"
        assert observations[0].author_name == "Alice"

    await engine.dispose()
```

- [ ] **Step 2: Run repository test and verify failure**

Run:

```bash
uv run pytest tests/test_comment_repositories.py -v
```

Expected: fails because `RawPageObservation`, `CommentEntity`, and repositories are not defined.

- [ ] **Step 3: Add ORM models**

Modify imports in `books_of_time/db/models.py`:

```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
```

Append these models after `RawPayload` indexes and before `VideoMetricSnapshot`:

```python
class RawPageObservation(Base):
    __tablename__ = "raw_page_observations"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    raw_payload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    request_type: Mapped[BilibiliRequestType] = mapped_column(
        Enum(BilibiliRequestType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    cursor: Mapped[str | None] = mapped_column(Text)
    sort_mode: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_raw_page_observations_target_time",
    RawPageObservation.target_type,
    RawPageObservation.target_id,
    RawPageObservation.captured_at.desc(),
)
Index("idx_raw_page_observations_raw_payload", RawPageObservation.raw_payload_id)


class CommentEntity(TimestampMixin, Base):
    __tablename__ = "comment_entities"

    rpid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    root_rpid: Mapped[int | None] = mapped_column(BigInteger)
    parent_rpid: Mapped[int | None] = mapped_column(BigInteger)
    author_mid: Mapped[int | None] = mapped_column(BigInteger)
    author_name: Mapped[str | None] = mapped_column(Text)
    first_content: Mapped[str | None] = mapped_column(Text)
    first_content_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)


Index("idx_comment_entities_bvid_rpid", CommentEntity.bvid, CommentEntity.rpid)
Index("idx_comment_entities_author_mid", CommentEntity.author_mid)


class CommentObservation(Base):
    __tablename__ = "comment_observations"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    rpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)
    raw_page_observation_id: Mapped[int | None] = mapped_column(BigInteger)
    sort_mode: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    reply_count: Mapped[int | None] = mapped_column(BigInteger)
    author_mid: Mapped[int | None] = mapped_column(BigInteger)
    author_name: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="visible")
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_comment_observations_bvid_time",
    CommentObservation.bvid,
    CommentObservation.captured_at.desc(),
)
Index(
    "idx_comment_observations_rpid_time",
    CommentObservation.rpid,
    CommentObservation.captured_at.desc(),
)
Index(
    "idx_comment_observations_raw_page",
    CommentObservation.raw_page_observation_id,
)
```

- [ ] **Step 4: Add repository classes**

Modify imports in `books_of_time/db/repositories.py`:

```python
from books_of_time.db.models import (
    CollectionTask,
    CommentEntity,
    CommentObservation,
    RawPageObservation,
    RawPayload,
    VideoMetricSnapshot,
)
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedComment,
    ParsedCommentPage,
)
```

Append these classes before `_hash_params`:

```python
class RawPageObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_parsed_page(
        self,
        parsed: ParsedCommentPage,
        *,
        request_type: BilibiliRequestType,
    ) -> RawPageObservation:
        observation = RawPageObservation(
            raw_payload_id=parsed.raw_payload_id,
            captured_at=parsed.captured_at,
            request_type=request_type,
            target_type="video",
            target_id=parsed.bvid,
            page_number=parsed.page_number,
            cursor=None,
            sort_mode=parsed.sort_mode,
            parser_version=COMMENT_PARSER_VERSION,
            status="success",
            item_count=len(parsed.comments),
            extra=parsed.extra,
        )
        self.session.add(observation)
        await self.session.flush()
        return observation


class CommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_page(
        self,
        parsed: ParsedCommentPage,
        *,
        raw_page_observation_id: int,
    ) -> list[CommentObservation]:
        observations: list[CommentObservation] = []
        for comment in parsed.comments:
            await self._ensure_entity(
                comment,
                captured_at=parsed.captured_at,
                raw_payload_id=parsed.raw_payload_id,
            )
            observation = CommentObservation(
                rpid=comment.rpid,
                bvid=comment.bvid,
                oid=comment.oid,
                captured_at=parsed.captured_at,
                raw_payload_id=parsed.raw_payload_id,
                raw_page_observation_id=raw_page_observation_id,
                sort_mode=parsed.sort_mode,
                page_number=parsed.page_number,
                position=comment.position,
                content=comment.content,
                content_hash=comment.content_hash,
                like_count=comment.like_count,
                reply_count=comment.reply_count,
                author_mid=comment.author_mid,
                author_name=comment.author_name,
                is_deleted=False,
                visibility="visible",
                extra={},
            )
            self.session.add(observation)
            observations.append(observation)
        await self.session.flush()
        return observations

    async def _ensure_entity(
        self,
        comment: ParsedComment,
        *,
        captured_at: datetime,
        raw_payload_id: int,
    ) -> CommentEntity:
        entity = await self.session.get(CommentEntity, comment.rpid)
        if entity is not None:
            entity.updated_at = captured_at
            return entity

        entity = CommentEntity(
            rpid=comment.rpid,
            oid=comment.oid,
            bvid=comment.bvid,
            root_rpid=comment.root_rpid,
            parent_rpid=comment.parent_rpid,
            author_mid=comment.author_mid,
            author_name=comment.author_name,
            first_content=comment.content,
            first_content_hash=comment.content_hash,
            first_seen_at=captured_at,
            first_raw_payload_id=raw_payload_id,
            created_at=captured_at,
            updated_at=captured_at,
        )
        self.session.add(entity)
        await self.session.flush()
        return entity
```

Also add this import to `books_of_time/db/repositories.py` if missing:

```python
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
```

- [ ] **Step 5: Run repository test and verify pass**

Run:

```bash
uv run pytest tests/test_comment_repositories.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Run related existing tests**

Run:

```bash
uv run pytest tests/test_task_queue.py tests/test_video_stats_worker.py tests/test_comment_repositories.py -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit model and repository task**

Run:

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py tests/test_comment_repositories.py
git commit -m "feat: store comment page observations"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 3: Bilibili Hot Comment Request Capture

**Files:**
- Modify: `books_of_time/platforms/bilibili/client.py`
- Test: `tests/test_bilibili_client.py`

**Interfaces:**
- Consumes existing `capture_bili_api_requests` and `classify_bilibili_request`.
- Produces `BilibiliPlatformClient.get_hot_comments(*, aid: int, page: int = 1) -> FetchResult`.

- [ ] **Step 1: Add failing platform client test**

Append to `tests/test_bilibili_client.py`:

```python
class FakeCommentResourceType:
    VIDEO = type("VideoType", (), {"value": 1})()


class FakeCommentOrderType:
    LIKE = type("LikeOrder", (), {"value": 2})()


async def fake_get_comments(oid, type_, page_index, order):
    from bilibili_api.utils.network import get_client

    response = await get_client().request(
        method="GET",
        url="https://api.bilibili.com/x/v2/reply",
        params={
            "oid": oid,
            "type": type_.value,
            "pn": page_index,
            "sort": order.value,
        },
        headers={},
        cookies={},
    )
    return response.json()["data"]


@pytest.mark.asyncio
async def test_hot_comments_uses_bilibili_api_client_backend(monkeypatch) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "books_of_time.platforms.bilibili.client.comment.CommentResourceType",
        FakeCommentResourceType,
    )
    monkeypatch.setattr(
        "books_of_time.platforms.bilibili.client.comment.OrderType",
        FakeCommentOrderType,
    )
    monkeypatch.setattr(
        "books_of_time.platforms.bilibili.client.comment.get_comments",
        fake_get_comments,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_hot_comments(aid=777, page=1)

    assert result.request_type == BilibiliRequestType.COMMENT_HOT
    assert raw_http_client.requests[0]["url"].endswith("/x/v2/reply")
    assert raw_http_client.requests[0]["params"]["oid"] == 777
    assert raw_http_client.requests[0]["params"]["pn"] == 1
    assert raw_http_client.requests[0]["params"]["sort"] == 2
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:comment_hot",
    ]
```

- [ ] **Step 2: Run platform test and verify failure**

Run:

```bash
uv run pytest tests/test_bilibili_client.py::test_hot_comments_uses_bilibili_api_client_backend -v
```

Expected: fails with `AttributeError: 'BilibiliPlatformClient' object has no attribute 'get_hot_comments'`.

- [ ] **Step 3: Implement platform client method**

Modify imports in `books_of_time/platforms/bilibili/client.py`:

```python
from bilibili_api import comment, user, video
```

Append method inside `BilibiliPlatformClient`:

```python
    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.LIKE,
            )
            return request_context.latest_result(BilibiliRequestType.COMMENT_HOT)
```

- [ ] **Step 4: Run platform tests and verify pass**

Run:

```bash
uv run pytest tests/test_bilibili_client.py -v
```

Expected: all platform client tests pass.

- [ ] **Step 5: Commit platform task**

Run:

```bash
git add books_of_time/platforms/bilibili/client.py tests/test_bilibili_client.py
git commit -m "feat: capture hot comment requests"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 4: Hot Comment Collector And Worker Registration

**Files:**
- Create: `books_of_time/collectors/hot_comments.py`
- Modify: `books_of_time/app.py`
- Test: `tests/test_hot_comments_worker.py`

**Interfaces:**
- Consumes:
  - `BilibiliPlatformClient.get_video_stats(bvid: str) -> FetchResult`
  - `BilibiliPlatformClient.get_hot_comments(aid: int, page: int = 1) -> FetchResult`
  - `RawPayloadRepository.insert_from_fetch_result`
  - `RawPageObservationRepository.insert_from_parsed_page`
  - `CommentRepository.upsert_page`
  - `parse_hot_comment_page`
- Produces:
  - `HotCommentsClient` protocol
  - `HotCommentCollector.collect(task: CollectionTask, session: AsyncSession) -> None`
  - worker registration for `TaskKind.FETCH_HOT_COMMENTS`

- [ ] **Step 1: Write worker integration test**

Create `tests/test_hot_comments_worker.py`:

```python
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.hot_comments import HotCommentCollector
from books_of_time.db.models import (
    Base,
    CollectionTask,
    CommentEntity,
    CommentObservation,
    RawPageObservation,
    RawPayload,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.worker import Worker


class FakeBilibiliClient:
    async def get_video_stats(self, bvid: str) -> FetchResult:
        body = json.dumps(
            {
                "code": 0,
                "data": {
                    "aid": 777,
                    "bvid": bvid,
                    "stat": {
                        "view": 1,
                        "like": 1,
                        "coin": 0,
                        "favorite": 0,
                        "share": 0,
                        "reply": 1,
                        "danmaku": 0,
                    },
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
        body = json.dumps(
            {
                "code": 0,
                "data": {
                    "cursor": {"all_count": 1},
                    "replies": [
                        {
                            "rpid": 1001,
                            "oid": aid,
                            "root": 0,
                            "parent": 0,
                            "like": 12,
                            "rcount": 3,
                            "member": {"mid": "42", "uname": "Alice"},
                            "content": {"message": "first comment"},
                        }
                    ],
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.COMMENT_HOT,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply",
            params={"oid": aid, "pn": page, "sort": 2},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_worker_fetch_hot_comments_archives_raw_and_writes_observations(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV1abc",
            priority=80,
            payload={"bvid": "BV1abc", "mode": "hot", "page": 1},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.FETCH_HOT_COMMENTS: HotCommentCollector(
                client=FakeBilibiliClient(),
                raw_store=RawPayloadFileStore(tmp_path),
                run_id="test-run",
            )
        },
        lease_owner="worker-test",
    )

    executed = await worker.run_once(now=now)
    assert executed is True

    async with session_factory() as session:
        task = await session.scalar(select(CollectionTask))
        raw_payloads = (
            await session.scalars(select(RawPayload).order_by(RawPayload.id.asc()))
        ).all()
        raw_page = await session.scalar(select(RawPageObservation))
        entity = await session.scalar(select(CommentEntity))
        observation = await session.scalar(select(CommentObservation))

        assert task.status == TaskStatus.SUCCEEDED
        assert len(raw_payloads) == 2
        assert raw_payloads[0].request_type == BilibiliRequestType.VIDEO_STATS
        assert raw_payloads[1].request_type == BilibiliRequestType.COMMENT_HOT
        assert raw_page is not None
        assert raw_page.raw_payload_id == raw_payloads[1].id
        assert raw_page.target_id == "BV1abc"
        assert raw_page.sort_mode == "hot"
        assert raw_page.item_count == 1
        assert entity is not None
        assert entity.rpid == 1001
        assert entity.author_mid == 42
        assert entity.author_name == "Alice"
        assert observation is not None
        assert observation.rpid == 1001
        assert observation.raw_payload_id == raw_payloads[1].id
        assert observation.raw_page_observation_id == raw_page.id
        assert observation.content == "first comment"

    await engine.dispose()
```

- [ ] **Step 2: Run worker test and verify failure**

Run:

```bash
uv run pytest tests/test_hot_comments_worker.py -v
```

Expected: fails with `ModuleNotFoundError: No module named 'books_of_time.collectors.hot_comments'`.

- [ ] **Step 3: Implement hot comment collector**

Create `books_of_time/collectors/hot_comments.py`:

```python
from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CollectionTask
from books_of_time.db.repositories import (
    CommentRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    parse_hot_comment_page,
)
from books_of_time.storage.filesystem import RawPayloadFileStore


class HotCommentsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult: ...


class HotCommentCollector:
    def __init__(
        self,
        *,
        client: HotCommentsClient,
        raw_store: RawPayloadFileStore,
        run_id: str,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id

    async def collect(self, task: CollectionTask, session: AsyncSession) -> None:
        bvid = str(task.payload.get("bvid") or task.target_id)
        page = int(task.payload.get("page") or 1)
        aid = task.payload.get("aid")

        if aid is None:
            video_result = await self.client.get_video_stats(bvid)
            video_raw = await self._archive_raw(video_result, session)
            video_payload = json.loads(video_result.body)
            aid = _extract_aid(video_payload)
            task.payload = {**task.payload, "aid": aid, "video_raw_payload_id": video_raw.id}

        comments_result = await self.client.get_hot_comments(aid=int(aid), page=page)
        comments_raw = await self._archive_raw(comments_result, session)
        parsed = parse_hot_comment_page(
            json.loads(comments_result.body),
            bvid=bvid,
            oid=int(aid),
            captured_at=comments_result.captured_at,
            raw_payload_id=comments_raw.id,
            page_number=page,
        )
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
        )

    async def _archive_raw(
        self,
        result: FetchResult,
        session: AsyncSession,
    ):
        stored = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=".json",
        )
        return await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored,
            parser_version=COMMENT_PARSER_VERSION
            if result.request_type == BilibiliRequestType.COMMENT_HOT
            else None,
        )


def _extract_aid(payload: dict) -> int:
    data = payload.get("data") or {}
    aid = data.get("aid")
    if aid is None:
        raise ValueError("Video info payload does not contain data.aid")
    return int(aid)
```

- [ ] **Step 4: Register collector in app factory**

Modify `books_of_time/app.py` imports:

```python
from books_of_time.collectors.hot_comments import HotCommentCollector
```

Modify `build_worker` collector map:

```python
        collectors={
            TaskKind.FETCH_VIDEO_STATS: VideoStatsCollector(
                client=client,
                raw_store=RawPayloadFileStore(raw_dir),
                run_id=run_id,
            ),
            TaskKind.FETCH_HOT_COMMENTS: HotCommentCollector(
                client=client,
                raw_store=RawPayloadFileStore(raw_dir),
                run_id=run_id,
            ),
        },
```

- [ ] **Step 5: Run worker test and verify pass**

Run:

```bash
uv run pytest tests/test_hot_comments_worker.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Run collector-adjacent tests**

Run:

```bash
uv run pytest tests/test_hot_comments_worker.py tests/test_video_stats_worker.py tests/test_comment_repositories.py -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit collector task**

Run:

```bash
git add books_of_time/collectors/hot_comments.py books_of_time/app.py tests/test_hot_comments_worker.py
git commit -m "feat: collect hot comment pages"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 5: CLI Enqueue, Tracking Update, And Final Verification

**Files:**
- Modify: `books_of_time/cli.py`
- Modify: `docs/TODO.md`
- Test: selected existing tests, then full suite

**Interfaces:**
- Consumes:
  - `TaskKind.FETCH_HOT_COMMENTS`
  - `CollectionTaskRepository.enqueue`
- Produces:
  - CLI command `bot video comments BVxxxx --mode hot --priority 80`
  - task payload `{"bvid": bvid, "mode": "hot", "page": 1}`
  - checked Phase 1A entries in `docs/TODO.md`

- [ ] **Step 1: Add CLI enqueue helper**

Modify `books_of_time/cli.py` by adding a nested `video comments` command in `build_parser()`:

```python
    video = subparsers.add_parser("video")
    video_sub = video.add_subparsers(dest="video_command", required=True)
    comments = video_sub.add_parser("comments")
    comments.add_argument("bvid")
    comments.add_argument("--mode", choices=["hot"], default="hot")
    comments.add_argument("--priority", type=int, default=80)
```

Add this branch in `_run()` after the `monitor-video` branch:

```python
    if args.command == "video" and args.video_command == "comments":
        await _enqueue_video_comments(cfg, args.bvid, args.mode, args.priority)
        return
```

Add helper:

```python
async def _enqueue_video_comments(
    cfg: dict,
    bvid: str,
    mode: str,
    priority: int,
) -> None:
    if mode != "hot":
        raise ValueError(f"Unsupported comment mode: {mode}")

    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id=bvid,
            priority=priority,
            payload={"bvid": bvid, "mode": mode, "page": 1},
            not_before=datetime.now(UTC),
        )
        await session.commit()
    logger.info("Queued hot comments task for %s", bvid)
```

- [ ] **Step 2: Run CLI parser smoke command**

Run:

```bash
uv run python -c "from books_of_time.cli import build_parser; args = build_parser().parse_args(['video', 'comments', 'BV1abc', '--mode', 'hot']); assert args.command == 'video'; assert args.video_command == 'comments'; assert args.mode == 'hot'; assert args.priority == 80"
```

Expected: command exits 0.

- [ ] **Step 3: Run CLI-adjacent tests**

Run:

```bash
uv run pytest tests/test_task_queue.py tests/test_hot_comments_worker.py -v
```

Expected: selected tests pass.

- [ ] **Step 4: Update tracking document**

Modify `docs/TODO.md` Phase 1A-relevant checkboxes:

```markdown
## P0: Request Layer And Raw Evidence

- [x] 建立 `raw_page_observations` 表。

## P1: Hot Comments

- [x] 调研 bilibili-api-python 评论接口，确认热门评论和最新评论方法。
- [x] 建立 `comment_entities` ORM 表。
- [x] 建立 `comment_observations` ORM 表。
- [x] 建立热门评论 parser。
- [x] 建立评论 content hash，并保留公开用户字段用于核验。
- [x] 实现 `HotCommentCollector`。
- [x] 支持热门评论第一页采集。
- [x] 写入 raw page observation。
- [x] 写入 comment entities。
- [x] 写入 comment observations。
- [x] CLI 支持 `bot video comments BVxxxx --mode hot`。
- [x] 测试同一 rpid 多次观测不会重复创建 entity。
```

Leave `支持按视频 tier 配置热门评论页数。` unchecked because Phase 1A only supports page 1.

- [ ] **Step 5: Run full verification**

Run:

```bash
uv run pytest
uv run ruff check .
```

Expected:

```text
pytest: all tests passed
ruff: All checks passed!
```

- [ ] **Step 6: Commit CLI and tracking task**

Run:

```bash
git add books_of_time/cli.py docs/TODO.md
git commit -m "feat: enqueue hot comment collection"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged unless the user explicitly asks to include it.

---

## Plan Self-Review

Spec coverage:

- Hot comments page 1 parser: Task 1.
- `raw_page_observations`, `comment_entities`, `comment_observations`: Task 2.
- Public author fields without anonymization: Task 1 parser fields and Task 2 model fields.
- Content hash while preserving readable content: Task 1 and Task 2.
- `BilibiliPlatformClient.get_hot_comments(aid, page=1)`: Task 3.
- `HotCommentCollector`: Task 4.
- Worker registration: Task 4.
- CLI `bot video comments BVxxxx --mode hot`: Task 5.
- Repository idempotence for same `rpid`: Task 2.
- Final verification with pytest and Ruff: Task 5.
- Tracking update: Task 5.

Type consistency:

- Parser uses `extra`, repositories write `extra`, and ORM models map `extra`; no `metadata` ORM attribute is introduced.
- Parser and repository agree on `content_hash: bytes` and `first_content_hash: bytes`.
- Platform and collector agree on `get_hot_comments(self, *, aid: int, page: int = 1)`.
- Collector, CLI, and tests agree on task kind `TaskKind.FETCH_HOT_COMMENTS` and payload keys `bvid`, `mode`, `page`, and optional `aid`.

Known intentional limits:

- Latest comments frontier is not implemented in this plan.
- Multi-page hot comments by tier is not implemented in this plan.
- Request failure taxonomy and persistent backoff states are not implemented in this plan.
- Coverage/run summary tables are not implemented in this plan.
