"""PostgreSQL BRIN indexes for append-oriented time tables.

Revision ID: 0005_brin_time_indexes
Revises: 0004_comment_analysis_flags
Create Date: 2026-07-11 10:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_brin_time_indexes"
down_revision: str | Sequence[str] | None = "0004_comment_analysis_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("idx_raw_payloads_captured_brin", "raw_payloads", "captured_at"),
    (
        "idx_raw_page_observations_captured_brin",
        "raw_page_observations",
        "captured_at",
    ),
    (
        "idx_comment_observations_captured_brin",
        "comment_observations",
        "captured_at",
    ),
    (
        "idx_video_metric_snapshots_captured_brin",
        "video_metric_snapshots",
        "captured_at",
    ),
    (
        "idx_video_info_snapshots_captured_brin",
        "video_info_snapshots",
        "captured_at",
    ),
    (
        "idx_video_availability_snapshots_captured_brin",
        "video_availability_snapshots",
        "captured_at",
    ),
    (
        "idx_comment_state_events_created_brin",
        "comment_state_events",
        "created_at",
    ),
    (
        "idx_comment_visibility_events_created_brin",
        "comment_visibility_events",
        "created_at",
    ),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, table, column in _INDEXES:
        op.create_index(
            name,
            table,
            [column],
            unique=False,
            postgresql_using="brin",
            postgresql_with={"pages_per_range": 128, "autosummarize": True},
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, table, _column in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
