from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CommentObservation, RawPageObservation
from books_of_time.domain.enums import BilibiliRequestType


@dataclass(frozen=True, slots=True)
class HotCommentTurnoverPoint:
    bvid: str
    top_n: int
    previous_at: datetime
    current_at: datetime
    previous_raw_page_id: int
    current_raw_page_id: int
    previous_rpids: tuple[int, ...]
    current_rpids: tuple[int, ...]
    retained_count: int
    entered_rpids: tuple[int, ...]
    exited_rpids: tuple[int, ...]
    turnover_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "hot-comment-turnover-v1",
            "bvid": self.bvid,
            "top_n": self.top_n,
            "previous_at": self.previous_at.isoformat(),
            "current_at": self.current_at.isoformat(),
            "previous_raw_page_id": self.previous_raw_page_id,
            "current_raw_page_id": self.current_raw_page_id,
            "previous_rpids": list(self.previous_rpids),
            "current_rpids": list(self.current_rpids),
            "retained_count": self.retained_count,
            "entered_rpids": list(self.entered_rpids),
            "exited_rpids": list(self.exited_rpids),
            "turnover_rate": self.turnover_rate,
        }


class HotCommentTurnoverAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        bvid: str,
        since: datetime,
        until: datetime,
        top_n: int = 20,
    ) -> list[HotCommentTurnoverPoint]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if top_n < 1 or top_n > 20:
            raise ValueError("top_n must be between 1 and 20")

        pages = list(
            await self.session.scalars(
                select(RawPageObservation)
                .where(
                    RawPageObservation.request_type == BilibiliRequestType.COMMENT_HOT,
                    RawPageObservation.target_type == "video",
                    RawPageObservation.target_id == bvid,
                    RawPageObservation.sort_mode == "hot",
                    RawPageObservation.page_number == 1,
                    RawPageObservation.status == "success",
                    RawPageObservation.captured_at >= since_utc,
                    RawPageObservation.captured_at < until_utc,
                )
                .order_by(
                    RawPageObservation.captured_at.asc(),
                    RawPageObservation.id.asc(),
                )
            )
        )
        page_ids = [page.id for page in pages]
        observations = list(
            await self.session.scalars(
                select(CommentObservation)
                .where(
                    CommentObservation.raw_page_observation_id.in_(page_ids),
                    CommentObservation.sort_mode == "hot",
                    CommentObservation.position.is_not(None),
                    CommentObservation.position <= top_n,
                )
                .order_by(
                    CommentObservation.raw_page_observation_id.asc(),
                    CommentObservation.position.asc(),
                    CommentObservation.rpid.asc(),
                )
            )
        )
        rpids_by_page: dict[int, list[int]] = {page_id: [] for page_id in page_ids}
        for observation in observations:
            page_rpids = rpids_by_page[observation.raw_page_observation_id]
            if observation.rpid not in page_rpids:
                page_rpids.append(observation.rpid)
        snapshots = [(page, tuple(rpids_by_page[page.id])) for page in pages]

        points: list[HotCommentTurnoverPoint] = []
        for previous, current in pairwise(snapshots):
            previous_page, previous_rpids = previous
            current_page, current_rpids = current
            previous_set = set(previous_rpids)
            current_set = set(current_rpids)
            entered = tuple(rpid for rpid in current_rpids if rpid not in previous_set)
            exited = tuple(rpid for rpid in previous_rpids if rpid not in current_set)
            points.append(
                HotCommentTurnoverPoint(
                    bvid=bvid,
                    top_n=top_n,
                    previous_at=previous_page.captured_at,
                    current_at=current_page.captured_at,
                    previous_raw_page_id=previous_page.id,
                    current_raw_page_id=current_page.id,
                    previous_rpids=previous_rpids,
                    current_rpids=current_rpids,
                    retained_count=len(previous_set & current_set),
                    entered_rpids=entered,
                    exited_rpids=exited,
                    turnover_rate=1
                    - (
                        len(previous_set & current_set)
                        / max(len(previous_set), len(current_set), 1)
                    ),
                )
            )
        return points


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
