"""persistent comment analysis flags

Revision ID: 0004_comment_analysis_flags
Revises: 0003_account_cookie_refresh_job
Create Date: 2026-07-11 01:15:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

import books_of_time.db.types
from alembic import op

revision: str = "0004_comment_analysis_flags"
down_revision: str | Sequence[str] | None = "0003_account_cookie_refresh_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comment_analysis_flags",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("flag_type", sa.String(length=64), nullable=False),
        sa.Column("subject_rpid", sa.BigInteger(), nullable=False),
        sa.Column("related_rpid", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("algorithm", sa.String(length=64), nullable=False),
        sa.Column("algorithm_version", sa.String(length=160), nullable=False),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("detected_at", books_of_time.db.types.UTCDateTime(), nullable=False),
        sa.Column(
            "created_at",
            books_of_time.db.types.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["subject_rpid"], ["comment_entities.rpid"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["related_rpid"], ["comment_entities.rpid"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stable_key"),
    )
    op.create_index(
        "idx_comment_analysis_flags_event_type",
        "comment_analysis_flags",
        ["event_id", "flag_type", "detected_at"],
        unique=False,
    )
    op.create_index(
        "idx_comment_analysis_flags_subject",
        "comment_analysis_flags",
        ["subject_rpid"],
        unique=False,
    )
    op.create_index(
        "idx_comment_analysis_flags_related",
        "comment_analysis_flags",
        ["related_rpid"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_comment_analysis_flags_related", table_name="comment_analysis_flags"
    )
    op.drop_index(
        "idx_comment_analysis_flags_subject", table_name="comment_analysis_flags"
    )
    op.drop_index(
        "idx_comment_analysis_flags_event_type",
        table_name="comment_analysis_flags",
    )
    op.drop_table("comment_analysis_flags")
