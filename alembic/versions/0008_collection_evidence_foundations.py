"""collection evidence foundations

Revision ID: 0008_collection_evidence_foundations
Revises: 0007_operational_alert_states
Create Date: 2026-07-14 02:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

import books_of_time.db.types
from alembic import op

revision: str = "0008_collection_evidence_foundations"
down_revision: str | Sequence[str] | None = "0007_operational_alert_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUEST_TYPE_VALUES = (
    "bilibili:video_info",
    "bilibili:video_stats",
    "bilibili:comment_hot",
    "bilibili:comment_latest",
    "bilibili:comment_reply",
    "bilibili:media_image",
    "bilibili:user_video_list",
    "bilibili:search_video",
    "bilibili:default",
)


def _request_type_enum() -> TypeEngine:
    return sa.Enum(
        *_REQUEST_TYPE_VALUES,
        name="bilibilirequesttype",
    ).with_variant(
        postgresql.ENUM(
            *_REQUEST_TYPE_VALUES,
            name="bilibilirequesttype",
            create_type=False,
        ),
        "postgresql",
    )


def _add_comment_evidence_columns(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "platform_created_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
    )
    op.add_column(table_name, sa.Column("author_level", sa.Integer(), nullable=True))
    op.add_column(
        table_name,
        sa.Column("author_official_type", sa.Integer(), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("author_official_description", sa.Text(), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("author_vip_status", sa.Integer(), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("author_vip_type", sa.Integer(), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("author_is_senior_member", sa.Boolean(), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column(
            "author_public_metadata_extra",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=False,
        )

    _add_comment_evidence_columns("comment_entities")
    _add_comment_evidence_columns("comment_observations")

    op.create_table(
        "known_video_sources",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("bvid", sa.Text(), nullable=False),
        sa.Column("source_mid", sa.Text(), nullable=False),
        sa.Column("pool_type", sa.String(length=32), nullable=False),
        sa.Column("pool_id", sa.Text(), nullable=False),
        sa.Column("game_id", sa.String(length=120), nullable=True),
        sa.Column("official", sa.Boolean(), nullable=False),
        sa.Column("monitored", sa.Boolean(), nullable=False),
        sa.Column(
            "first_seen_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column("first_raw_page_id", sa.BigInteger(), nullable=True),
        sa.Column("last_raw_page_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            books_of_time.db.types.UTCDateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="记录创建时间",
        ),
        sa.Column(
            "updated_at",
            books_of_time.db.types.UTCDateTime(),
            server_default=sa.func.now(),
            nullable=False,
            comment="记录最后更新时间",
        ),
        sa.ForeignKeyConstraint(
            ["bvid"],
            ["known_videos.bvid"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["first_raw_page_id"],
            ["raw_page_observations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_raw_page_id"],
            ["raw_page_observations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bvid",
            "source_mid",
            "pool_type",
            "pool_id",
            name="uq_known_video_sources_identity",
        ),
    )
    op.create_index(
        "idx_known_video_sources_bvid_active",
        "known_video_sources",
        ["bvid", "active"],
        unique=False,
    )
    op.create_index(
        "idx_known_video_sources_game_flags",
        "known_video_sources",
        ["game_id", "official", "monitored"],
        unique=False,
    )
    op.create_index(
        "idx_known_video_sources_mid",
        "known_video_sources",
        ["source_mid"],
        unique=False,
    )

    op.create_table(
        "http_request_attempts",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("collection_task_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_cohort_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_cohort_component_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request_type", _request_type_enum(), nullable=False),
        sa.Column(
            "attempt_started_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "request_started_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "request_finished_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "response_received_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("method", sa.String(length=12), nullable=False),
        sa.Column("url_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("params_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_payload_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["collection_task_id"],
            ["collection_tasks.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["raw_payload_id"],
            ["raw_payloads.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_http_request_attempts_status_time",
        "http_request_attempts",
        ["status", sa.text("attempt_started_at DESC")],
        unique=False,
    )
    op.create_index(
        "idx_http_request_attempts_task",
        "http_request_attempts",
        ["collection_task_id"],
        unique=False,
    )
    op.create_index(
        "idx_http_request_attempts_raw",
        "http_request_attempts",
        ["raw_payload_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_http_request_attempts_raw", table_name="http_request_attempts")
    op.drop_index("idx_http_request_attempts_task", table_name="http_request_attempts")
    op.drop_index(
        "idx_http_request_attempts_status_time",
        table_name="http_request_attempts",
    )
    op.drop_table("http_request_attempts")

    op.drop_index("idx_known_video_sources_mid", table_name="known_video_sources")
    op.drop_index(
        "idx_known_video_sources_game_flags",
        table_name="known_video_sources",
    )
    op.drop_index(
        "idx_known_video_sources_bvid_active",
        table_name="known_video_sources",
    )
    op.drop_table("known_video_sources")

    for table_name in ("comment_observations", "comment_entities"):
        op.drop_column(table_name, "author_public_metadata_extra")
        op.drop_column(table_name, "author_is_senior_member")
        op.drop_column(table_name, "author_vip_type")
        op.drop_column(table_name, "author_vip_status")
        op.drop_column(table_name, "author_official_description")
        op.drop_column(table_name, "author_official_type")
        op.drop_column(table_name, "author_level")
        op.drop_column(table_name, "platform_created_at")
