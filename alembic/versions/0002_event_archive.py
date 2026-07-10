"""event archive core

Revision ID: 0002_event_archive
Revises: 0001_initial
Create Date: 2026-07-10 18:51:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

import books_of_time.db.types
from alembic import op

revision: str = "0002_event_archive"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            comment="记录创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            comment="记录最后更新时间",
        ),
    )


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("game", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("start_at", books_of_time.db.types.UTCDateTime(), nullable=True),
        sa.Column("end_at", books_of_time.db.types.UTCDateTime(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("idx_events_game", "events", ["game"], unique=False)
    op.create_index(
        "idx_events_status_time",
        "events",
        ["status", "start_at"],
        unique=False,
    )

    op.create_table(
        "event_targets",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "first_seen_at", books_of_time.db.types.UTCDateTime(), nullable=False
        ),
        sa.Column("last_seen_at", books_of_time.db.types.UTCDateTime(), nullable=False),
        sa.Column(
            "extra",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "target_type",
            "normalized_value",
            name="uq_event_targets_stable_key",
        ),
    )
    op.create_index(
        "idx_event_targets_event_active",
        "event_targets",
        ["event_id", "active"],
        unique=False,
    )
    op.create_index(
        "idx_event_targets_type_value",
        "event_targets",
        ["target_type", "normalized_value"],
        unique=False,
    )

    op.create_table(
        "event_videos",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("bvid", sa.Text(), nullable=False),
        sa.Column("source_target_id", sa.BigInteger(), nullable=True),
        sa.Column("association_reason", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "first_seen_at", books_of_time.db.types.UTCDateTime(), nullable=False
        ),
        sa.Column("last_seen_at", books_of_time.db.types.UTCDateTime(), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_target_id"], ["event_targets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("event_id", "bvid"),
    )
    op.create_index("idx_event_videos_bvid", "event_videos", ["bvid"], unique=False)
    op.create_index(
        "idx_event_videos_event_active",
        "event_videos",
        ["event_id", "active"],
        unique=False,
    )

    op.create_table(
        "event_keywords",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("normalized_keyword", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("source_target_id", sa.BigInteger(), nullable=True),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_target_id"], ["event_targets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "normalized_keyword",
            "version",
            name="uq_event_keywords_version",
        ),
    )
    op.create_index(
        "idx_event_keywords_event_active",
        "event_keywords",
        ["event_id", "active"],
        unique=False,
    )
    op.create_index(
        "idx_event_keywords_normalized",
        "event_keywords",
        ["normalized_keyword"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_event_keywords_normalized", table_name="event_keywords")
    op.drop_index("idx_event_keywords_event_active", table_name="event_keywords")
    op.drop_table("event_keywords")
    op.drop_index("idx_event_videos_event_active", table_name="event_videos")
    op.drop_index("idx_event_videos_bvid", table_name="event_videos")
    op.drop_table("event_videos")
    op.drop_index("idx_event_targets_type_value", table_name="event_targets")
    op.drop_index("idx_event_targets_event_active", table_name="event_targets")
    op.drop_table("event_targets")
    op.drop_index("idx_events_status_time", table_name="events")
    op.drop_index("idx_events_game", table_name="events")
    op.drop_table("events")
